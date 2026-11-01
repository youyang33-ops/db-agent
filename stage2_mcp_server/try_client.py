"""阶段 2：用 SDK 的 Client 把 server 的每个功能调一遍，看清楚"线上传的到底是什么"。

运行：python -m stage2_mcp_server.try_client

没有安装 Node.js、用不了 MCP Inspector 时，用这个脚本代替。
它通过 stdio 启动 server 子进程 —— 和 Cursor、Claude Desktop 启动 MCP server 的方式完全一样。
"""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from common.config import PROJECT_ROOT

SERVER = StdioServerParameters(
    command=sys.executable,  # 用当前虚拟环境的 Python
    args=["-m", "stage2_mcp_server.server"],
    cwd=str(PROJECT_ROOT),
)


def show(title: str, obj: object) -> None:
    print(f"\n===== {title} =====")
    print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def main() -> None:
    async with Client(SERVER) as client:
        # 连接时双方已经交换过版本和能力（2026-07-28 版协议用 server/discover，旧版用 initialize 握手）
        show("协议版本", client.protocol_version)
        show("server 信息", client.server_info.model_dump() if client.server_info else None)
        show("server 给 Host 的使用说明（instructions）", client.instructions or "")

        tools = await client.list_tools()
        for tool in tools.tools:
            # input_schema 是 SDK 从函数签名自动生成的 —— 对比阶段 1 里手写的 TOOLS
            show(f"工具 {tool.name}", {"description": tool.description, "input_schema": tool.input_schema,
                                     "output_schema": tool.output_schema})

        result = await client.call_tool("run_sql", {"sql": "SELECT name, price FROM products LIMIT 3"})
        show("调用 run_sql：structured_content（给程序看的）", result.structured_content)
        show("调用 run_sql：content（给模型看的）", [c.text for c in result.content if isinstance(c, TextContent)])

        bad = await client.call_tool("run_sql", {"sql": "SELECT nme FROM products"})
        show("故意写错 SQL：is_error", bad.is_error)
        show("错误信息（模型能看到它并自己改正）", [c.text for c in bad.content if isinstance(c, TextContent)])

        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        show("资源", [str(r.uri) for r in resources.resources])
        show("资源模板", [t.uri_template for t in templates.resource_templates])
        content = await client.read_resource("schema://tables/orders")
        show("读取 schema://tables/orders", content.contents[0].text)  # type: ignore[union-attr]

        prompts = await client.list_prompts()
        show("提示词模板", [p.name for p in prompts.prompts])
        prompt = await client.get_prompt("analyze_sales", {"month": "2026-08"})
        show("渲染 analyze_sales", prompt.messages[0].content.text)  # type: ignore[union-attr]


if __name__ == "__main__":
    asyncio.run(main())
