"""阶段 2：把数据库包装成 MCP Server。

和阶段 1 对比：工具的 JSON Schema 不用手写了 —— SDK 读取函数的类型标注、默认值、Field 描述和 docstring 自动生成。
而且这个 server 是独立进程，Cursor、Cherry Studio、Claude Desktop 或你自己写的 Agent 都能直接用。

运行方式：
    python -m stage2_mcp_server.server          # stdio 模式：启动后什么都不输出，在等客户端从 stdin 发消息
    mcp dev stage2_mcp_server/server.py         # 用 MCP Inspector 调试（需要安装 Node.js）
    python -m stage2_mcp_server.try_client      # 用 SDK 自带的 Client 把每个功能调一遍

这个文件演示了 MCP Server 的三种原语：
- Tools：模型决定什么时候调用（list_tables / describe_table / run_sql）
- Resources：应用（Host）决定什么时候读取（schema://tables）
- Prompts：用户主动选择的模板（analyze_sales）
"""

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import Field

from common import db
from common.config import DEFAULT_DB
from common.db import QueryResult, SQLExecutor
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Message, UserMessage
from mcp.types import ToolAnnotations

# 日志会写到 stderr。stdio 模式下 stdout 是协议通道，千万不要往 stdout 打印东西。
logger = logging.getLogger("shop-db")

INSTRUCTIONS = """这个服务器提供一个电商 SQLite 数据库（商品、客户、订单、评价）的查询能力。
建议流程：先 list_tables 和 describe_table 了解表结构，再用 run_sql 执行 SELECT 查询。
订单只有 status='已完成' 才算有效销售。"""


def build_server(
    name: str = "shop-db",
    db_path: Path = DEFAULT_DB,
    executor: SQLExecutor = db.execute_query,
    **server_kwargs: Any,
) -> MCPServer:
    """创建 server 并注册所有功能。

    写成工厂函数，后面的阶段可以复用它：
    - 阶段 4 传入 token_verifier / auth，变成带鉴权的远程服务；
    - 阶段 5 传入安全版 executor，工具代码一行不改就完成加固。
    """

    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[None]:
        # lifespan：server 启动时执行 yield 之前的代码，关闭时执行之后的代码。
        # 真实项目里通常在这里创建数据库连接池、加载模型等"只做一次"的事情。
        if not db_path.exists():
            raise RuntimeError(f"数据库不存在：{db_path}，请先运行 python -m data.init_db")
        logger.info("shop-db 启动，数据库：%s", db_path)
        yield
        logger.info("shop-db 关闭")

    mcp = MCPServer(name, instructions=INSTRUCTIONS, lifespan=lifespan, **server_kwargs)

    # ------------------------------------------------------------------ Tools
    # read_only_hint 等注解是给 Host 看的提示（比如只读工具可以不弹确认框），不是强制约束。
    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    @mcp.tool(annotations=read_only)
    def list_tables() -> list[str]:
        """列出数据库里所有的表名。"""
        return db.list_tables(db_path)

    @mcp.tool(annotations=read_only)
    def describe_table(table: str = Field(description="表名，例如 orders")) -> str:
        """查看一张表的建表语句（字段名、类型和中文注释）。写 SQL 之前先调用它，不要猜字段名。"""
        ddl = db.table_ddl(db_path, table)
        if not ddl:
            # ToolError → 返回 is_error=True 的结果，模型能看到这句话并自己改正
            raise ToolError(f"表 {table!r} 不存在。可用的表：{', '.join(db.list_tables(db_path))}")
        return ddl

    @mcp.tool()
    def run_sql(
        sql: str = Field(description="一条 SQLite SELECT 语句"),
        limit: int = Field(default=50, ge=1, le=500, description="最多返回多少行"),
    ) -> QueryResult:
        """执行一条 SQL 查询，返回列名和数据行。结果超过 limit 行会被截断（truncated=true）。"""
        try:
            return executor(db_path, sql, limit)
        except sqlite3.Error as e:
            # 把数据库报错原样交给模型，并给出下一步建议 —— 好的错误信息能让模型自己修好 SQL
            hint = "。请用 describe_table 检查表名和字段名后重试" if "no such" in str(e) else ""
            raise ToolError(f"SQL 执行失败：{e}{hint}") from e

    # -------------------------------------------------------------- Resources
    # 资源是只读的上下文数据，由 Host 决定要不要读、什么时候读（例如启动时把表结构放进系统提示词）。

    @mcp.resource("schema://tables", mime_type="text/plain")
    def all_schemas() -> str:
        """所有表的建表语句。"""
        return db.table_ddl(db_path)

    @mcp.resource("schema://tables/{table}", mime_type="text/plain")
    def one_schema(table: str) -> str:
        """某一张表的建表语句（URI 模板，{table} 会被替换）。"""
        return db.table_ddl(db_path, table) or f"表 {table} 不存在"

    # ---------------------------------------------------------------- Prompts
    # 提示词模板由用户主动选择（在客户端里通常显示成斜杠命令，例如 /analyze_sales）。

    @mcp.prompt(title="月度销售分析")
    def analyze_sales(month: str = Field(description="月份，格式 YYYY-MM，例如 2026-08")) -> list[Message]:
        """生成一份指定月份的销售分析报告。"""
        return [
            UserMessage(
                f"请分析 {month} 的销售情况，依次给出：\n"
                f"1. 总销售额和订单数（只统计已完成订单）；\n"
                f"2. 销售额最高的 5 个商品；\n"
                f"3. 各类目的销售额占比；\n"
                f"4. 和上个月相比的变化，以及你的简短结论。\n"
                f"每个数字都必须来自 SQL 查询结果，不要估算。"
            )
        ]

    return mcp


# mcp dev / mcp run 会在模块里寻找名为 mcp 的对象
mcp = build_server()

if __name__ == "__main__":
    mcp.run()  # 默认 stdio
