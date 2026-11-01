"""数据库访问层。阶段 1~4 用这里的"朴素"实现，阶段 5 会换成加固版（见 stage5_security_eval/sql_guard.py）。"""

import sqlite3
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class QueryResult(BaseModel):
    """一次 SQL 查询的结果。用 Pydantic 模型做返回值，MCP 会自动生成 outputSchema（结构化输出）。"""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool  # 是否因为超过 limit 被截断


class SQLExecutor(Protocol):
    """执行 SQL 的函数签名。把它做成可替换的参数，阶段 5 才能在不改工具代码的前提下换成安全版。"""

    def __call__(self, db_path: Path, sql: str, limit: int) -> QueryResult: ...


def execute_query(db_path: Path, sql: str, limit: int) -> QueryResult:
    """朴素实现：读写连接、执行后 commit，不做任何限制。

    ⚠️ 故意留下的安全问题（阶段 5 修复）：模型传来 DROP / DELETE 也会照样执行并提交。
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit + 1) if columns else []
        conn.commit()
    finally:
        conn.close()
    truncated = len(rows) > limit
    rows = [list(r) for r in rows[:limit]]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


def list_tables(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def table_ddl(db_path: Path, table: str | None = None) -> str:
    """返回建表语句（含字段注释）。table 为空时返回所有表。"""
    conn = sqlite3.connect(db_path)
    try:
        if table is None:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0] for r in rows)
