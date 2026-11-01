"""阶段 1：不用 MCP，手写 Function Calling 循环。

运行：
    python -m stage1_function_calling.agent "销量最高的 3 个商品是什么？"
    python -m stage1_function_calling.agent            # 不带参数进入多轮对话

读这个文件时重点看三处：
1. TOOLS：工具的 JSON Schema 是手写的 —— 函数改了参数，这里也要跟着改（MCP 会帮你自动生成）。
2. TOOL_IMPLS：工具名到 Python 函数的映射 —— 工具和 Agent 写死在同一个进程里，别的应用没法复用。
3. run_agent()：Agent 循环的全部逻辑，后面每个阶段都是在这个循环上加东西。
"""

import asyncio
import json
import sys
from typing import Any, Callable

from common import db
from common.config import DEFAULT_DB
from common.llm import LLM, Message, create_llm

SYSTEM_PROMPT = """你是一个电商数据分析助手，可以查询一个 SQLite 数据库来回答问题。
- 先弄清楚有哪些表和字段，再写 SQL；不要编造字段名。
- 只用 SELECT 查询。
- 回答用中文，给出具体数字。"""

# 手写的工具描述（OpenAI function calling 格式）。模型就是靠 description 判断"什么时候该用哪个工具"。
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "列出数据库里所有的表名。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": "查看某张表的建表语句（字段名、类型和中文注释）。写 SQL 之前先调用它。",
            "parameters": {
                "type": "object",
                "properties": {"table": {"type": "string", "description": "表名"}},
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "执行一条 SQLite 的 SELECT 语句，返回列名和数据行（最多 limit 行）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "要执行的 SQL"},
                    "limit": {"type": "integer", "description": "最多返回多少行", "default": 50},
                },
                "required": ["sql"],
            },
        },
    },
]

TOOL_IMPLS: dict[str, Callable[..., Any]] = {
    "list_tables": lambda: db.list_tables(DEFAULT_DB),
    "describe_table": lambda table: db.table_ddl(DEFAULT_DB, table) or f"表 {table} 不存在",
    "run_sql": lambda sql, limit=50: db.execute_query(DEFAULT_DB, sql, limit).model_dump(),
}


def execute_tool(name: str, arguments_json: str) -> str:
    """执行一次工具调用，结果转成字符串交还给模型。出错也要返回给模型（而不是让程序崩掉），模型才能自己改正。"""
    try:
        args = json.loads(arguments_json or "{}")
        result = TOOL_IMPLS[name](**args)
    except KeyError:
        return f"错误：不存在名为 {name} 的工具"
    except Exception as e:  # SQL 写错、参数不对……
        return f"错误：{type(e).__name__}: {e}"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


async def run_agent(llm: LLM, messages: list[Message], max_steps: int = 8, verbose: bool = True) -> str:
    """Agent 循环：调用模型 → 如果它要用工具就执行 → 把结果放回对话 → 再调用模型，直到它给出最终回答。"""
    for step in range(1, max_steps + 1):
        reply = await llm.chat(messages, TOOLS)
        messages.append(reply)

        tool_calls = reply.get("tool_calls")
        if not tool_calls:  # 没有工具调用 = 模型认为可以回答了
            return reply["content"]

        for call in tool_calls:
            name, args = call["function"]["name"], call["function"]["arguments"]
            result = execute_tool(name, args)
            if verbose:
                print(f"  [第{step}步] 调用 {name}({args})\n         → {result[:200]}")
            # tool 消息必须带上对应的 tool_call_id，模型才知道这是哪次调用的结果
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    # 防止模型陷入"无限调用工具"的死循环
    return f"（已达到最大步数 {max_steps}，停止。）"


async def main() -> None:
    llm = create_llm()
    print(f"使用模型：{llm.name}")
    messages: list[Message] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if len(sys.argv) > 1:
        messages.append({"role": "user", "content": " ".join(sys.argv[1:])})
        print(await run_agent(llm, messages))
        return

    print("输入问题开始对话，输入 exit 退出。")
    while (question := input("\n你：").strip()) not in ("exit", "quit"):
        messages.append({"role": "user", "content": question})
        print("助手：" + await run_agent(llm, messages))


if __name__ == "__main__":
    asyncio.run(main())
