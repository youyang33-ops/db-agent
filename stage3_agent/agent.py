"""阶段 3：自己写一个 Agent Host —— 用 MCP Client 连接多个 server，把它们的工具交给大模型调度。

运行：
    python -m stage3_agent.agent                                   # 多轮对话
    python -m stage3_agent.agent "销量最高的 3 个商品是什么？保存成报告"
    python -m stage3_agent.agent --config 其它配置.json "问题"

对话里可以输入的命令：
    /tools                    列出所有可用工具
    /prompts                  列出所有提示词模板
    /prompt analyze_sales 2026-08   使用 server 提供的提示词模板提问（用户主动选择 = Prompts 原语）
    /reset                    清空对话历史
    exit                      退出

和阶段 1 的 run_agent() 对比，这里多了：
1. 工具不再写死：启动时从每个 MCP server 动态获取（list_tools），转换成 OpenAI function 格式；
2. 工具名加上 server 前缀（shop_db__run_sql），多个 server 有同名工具也不会冲突；
3. Host 自己决定读哪些 Resource 放进系统提示词（servers.json 里的 context_resources）；
4. 区分两种错误：工具执行失败（is_error，交给模型重试）和协议错误（MCPError）。
"""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from common.config import PROJECT_ROOT, env
from common.llm import LLM, Message, create_llm
from mcp import Client, MCPError, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, TextResourceContents, Tool

SYSTEM_PROMPT = """你是一个电商数据分析助手，通过工具查询数据库来回答问题。
- 写 SQL 前先确认表结构，不要编造表名和字段名；SQL 出错时根据错误信息修改后重试。
- 每个数字都必须来自工具返回的结果。
- 用户要求保存或生成报告时，用保存报告的工具把结果存下来。
- 回答用中文，简洁，给出具体数字。"""

DEFAULT_CONFIG = Path(__file__).with_name("servers.json")


@dataclass
class RemoteServer:
    """一个通过 Streamable HTTP 连接的远程 MCP server（阶段 4）。"""

    url: str
    token: str | None = None


@dataclass
class ServerTarget:
    target: Any  # StdioServerParameters / RemoteServer / MCPServer 实例（测试用，进程内直连）
    context_resources: list[str] = field(default_factory=list)


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    result: str


@dataclass
class AgentResult:
    answer: str
    steps: int
    tool_calls: list[ToolCallRecord]


def load_config(path: Path) -> dict[str, ServerTarget]:
    """读取 servers.json。格式和 Claude Desktop / Cursor 的 mcpServers 配置基本一致。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers: dict[str, ServerTarget] = {}
    for name, cfg in raw.items():
        if "url" in cfg:
            token = None
            if token_env := cfg.get("token_env"):
                # token 放在环境变量里，不要写进配置文件（配置文件经常会被提交到 Git）
                token = env(token_env)
                if not token:
                    raise ValueError(f"{name} 需要环境变量 {token_env}，请在 .env 里设置")
            target: Any = RemoteServer(cfg["url"], token)
        else:
            command = sys.executable if cfg["command"] == "python" else cfg["command"]
            # 注意：stdio 子进程默认不继承你的环境变量（只有 HOME、PATH 等少数几个），需要的要写在 env 里
            target = StdioServerParameters(
                command=command, args=cfg.get("args", []), env=cfg.get("env"), cwd=str(PROJECT_ROOT)
            )
        servers[name] = ServerTarget(target, cfg.get("context_resources", []))
    return servers


def to_openai_tool(full_name: str, tool: Tool) -> dict[str, Any]:
    """MCP 工具定义 → OpenAI function calling 格式。input_schema 本身就是 JSON Schema，可以直接用。"""
    return {
        "type": "function",
        "function": {
            "name": full_name,
            "description": tool.description or tool.title or tool.name,
            "parameters": tool.input_schema,
        },
    }


class MCPAgent:
    def __init__(
        self,
        llm: LLM,
        servers: dict[str, ServerTarget],
        *,
        system_prompt: str = SYSTEM_PROMPT,
        max_steps: int = 8,
        max_result_chars: int = 4000,
        elicitation_callback: Any = None,
        verbose: bool = True,
    ) -> None:
        self.llm = llm
        self.servers = servers
        self.base_system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_result_chars = max_result_chars
        self.elicitation_callback = elicitation_callback
        self.verbose = verbose

        self.clients: dict[str, Client] = {}
        self.tool_index: dict[str, tuple[Client, str]] = {}  # "shop_db__run_sql" → (client, "run_sql")
        self.tool_specs: list[dict[str, Any]] = []
        self.system_prompt = system_prompt
        self.messages: list[Message] = []
        self._stack = AsyncExitStack()

    # ------------------------------------------------------------ 连接管理
    async def __aenter__(self) -> "MCPAgent":
        context_parts: list[str] = []
        for name, server in self.servers.items():
            client = await self._stack.enter_async_context(
                Client(await self._open_transport(server.target), elicitation_callback=self.elicitation_callback)
            )
            self.clients[name] = client

            for tool in (await client.list_tools()).tools:
                full_name = f"{name}__{tool.name}"
                self.tool_index[full_name] = (client, tool.name)
                self.tool_specs.append(to_openai_tool(full_name, tool))

            # Resources 由 Host 决定读不读：这里按配置把表结构读进系统提示词，模型就不用每次都调用 describe_table
            if client.instructions:
                context_parts.append(f"## {name} 的使用说明\n{client.instructions}")
            for uri in server.context_resources:
                result = await client.read_resource(uri)
                text = "\n".join(c.text for c in result.contents if isinstance(c, TextResourceContents))
                context_parts.append(f"## {name} 提供的资源 {uri}\n{text}")

        if context_parts:
            self.system_prompt = self.base_system_prompt + "\n\n" + "\n\n".join(context_parts)
        self.reset()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()  # 关闭所有连接；stdio 的 server 子进程也会被结束

    async def _open_transport(self, target: Any) -> Any:
        if isinstance(target, RemoteServer):
            headers = {"Authorization": f"Bearer {target.token}"} if target.token else {}
            http = await self._stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0))
            )
            return streamable_http_client(target.url, http_client=http)
        return target

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    # ------------------------------------------------------------ 工具调用
    def format_result(self, result: CallToolResult) -> str:
        """把工具结果变成给模型看的文本。阶段 5 会重写这个方法，加上防提示注入的包装。"""
        parts = [c.text if isinstance(c, TextContent) else f"[{c.type} 内容已省略]" for c in result.content]
        text = "\n".join(parts)
        if len(text) > self.max_result_chars:  # 结果太长会撑爆上下文，截断并告诉模型
            text = text[: self.max_result_chars] + f"\n……（结果过长，已截断到 {self.max_result_chars} 字符，请缩小查询范围）"
        return ("【工具执行出错】" + text) if result.is_error else text

    async def call_tool(self, full_name: str, arguments_json: str) -> ToolCallRecord:
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as e:
            return ToolCallRecord(full_name, {}, True, f"【参数不是合法 JSON】{e}")
        if full_name not in self.tool_index:
            return ToolCallRecord(full_name, arguments, True, f"【不存在的工具】{full_name}")

        client, tool_name = self.tool_index[full_name]
        try:
            result = await client.call_tool(tool_name, arguments)
        except MCPError as e:
            # 协议层错误（工具不存在、参数校验失败、被拒绝……）。按规范这是给程序处理的；
            # 这里选择把它也转成文字告诉模型，让对话能继续，同时标记为失败。
            return ToolCallRecord(full_name, arguments, True, f"【协议错误 {e.code}】{e.message}")
        return ToolCallRecord(full_name, arguments, result.is_error, self.format_result(result))

    # ------------------------------------------------------------ Agent 循环
    async def ask(self, question: str) -> AgentResult:
        self.messages.append({"role": "user", "content": question})
        records: list[ToolCallRecord] = []

        for step in range(1, self.max_steps + 1):
            reply = await self.llm.chat(self.messages, self.tool_specs)
            self.messages.append(reply)
            if not reply.get("tool_calls"):
                return AgentResult(reply["content"], step, records)

            # 模型一次可能要求调用多个工具。这里按顺序执行；互不依赖时也可以用 asyncio.gather 并发执行。
            for call in reply["tool_calls"]:
                record = await self.call_tool(call["function"]["name"], call["function"]["arguments"])
                records.append(record)
                if self.verbose:
                    flag = "✗" if record.is_error else "✓"
                    print(f"  [第{step}步 {flag}] {record.tool}({json.dumps(record.arguments, ensure_ascii=False)})")
                    print(f"           → {record.result[:160].replace(chr(10), ' ')}")
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": record.result})

        return AgentResult(f"（已达到最大步数 {self.max_steps}，停止。）", self.max_steps, records)

    # ------------------------------------------------------------ Prompts
    async def render_prompt(self, name: str, arguments: dict[str, str]) -> str:
        """在所有 server 里找到这个提示词模板并渲染成文字。"""
        for client in self.clients.values():
            prompts = await client.list_prompts() if client.server_capabilities.prompts else None
            if prompts and any(p.name == name for p in prompts.prompts):
                result = await client.get_prompt(name, arguments)
                return "\n".join(m.content.text for m in result.messages if isinstance(m.content, TextContent))
        raise ValueError(f"没有找到提示词模板 {name}")

    async def list_prompts(self) -> list[str]:
        names = []
        for server, client in self.clients.items():
            if client.server_capabilities.prompts:
                for p in (await client.list_prompts()).prompts:
                    args = " ".join(f"<{a.name}>" for a in p.arguments or [])
                    names.append(f"{p.name} {args}  —— {p.description}（来自 {server}）")
        return names


async def chat_loop(agent: MCPAgent) -> None:
    print("输入问题开始对话；/tools /prompts /prompt <名称> <参数...> /reset；exit 退出。")
    while (line := input("\n你：").strip()) not in ("exit", "quit"):
        if not line:
            continue
        if line == "/tools":
            print("\n".join(f"- {t['function']['name']}: {t['function']['description']}" for t in agent.tool_specs))
            continue
        if line == "/prompts":
            print("\n".join(await agent.list_prompts()) or "（没有提示词模板）")
            continue
        if line == "/reset":
            agent.reset()
            print("（已清空对话历史）")
            continue
        if line.startswith("/prompt "):
            # 简化处理：参数按位置对应模板声明的参数
            name, *values = line.split()[1:]
            prompts = {p.split()[0]: p for p in await agent.list_prompts()}
            arg_names = [w.strip("<>") for w in prompts.get(name, "").split("——")[0].split()[1:]]
            line = await agent.render_prompt(name, dict(zip(arg_names, values)))
            print(f"（模板展开为）{line}")
        result = await agent.ask(line)
        print(f"助手：{result.answer}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="数据库问答 Agent（MCP 版）")
    parser.add_argument("question", nargs="*", help="直接提问；不填则进入多轮对话")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="MCP server 配置文件")
    args = parser.parse_args()

    llm = create_llm()
    async with MCPAgent(llm, load_config(args.config)) as agent:
        print(f"使用模型：{llm.name}；已连接：{', '.join(agent.clients)}；共 {len(agent.tool_specs)} 个工具")
        if args.question:
            result = await agent.ask(" ".join(args.question))
            print(f"\n助手：{result.answer}")
        else:
            await chat_loop(agent)


if __name__ == "__main__":
    asyncio.run(main())
