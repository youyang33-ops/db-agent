"""阶段 5：安全版 SQL 执行器。

核心思想：**不要指望模型守规矩，要在工具层强制执行规则**。
提示词里写"只能 SELECT"只是建议，模型可能被提示注入骗过去；下面这四层防护模型绕不过去：

1. 只读连接（mode=ro）：数据库层面就不允许写入；
2. SQLite authorizer：每个操作执行前都要问一下回调函数，只放行 SELECT/读取，DROP/INSERT/ATTACH/PRAGMA 全部拒绝；
3. 敏感字段脱敏：customers.phone 读出来一律是 NULL（最小权限：模型根本不需要手机号）；
4. 超时 + 行数上限：防止一条笛卡尔积查询把服务拖垮。
"""

import sqlite3
import time
from pathlib import Path

from common.db import QueryResult

MAX_ROWS = 200
TIMEOUT_SECONDS = 2.0

# 敏感字段：(表名, 字段名)
MASKED_COLUMNS = {("customers", "phone")}

ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,  # 允许 COUNT、SUM、substr 等函数
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # 允许 WITH RECURSIVE
}

# authorizer 动作码 → 名字（报错时告诉模型是哪个操作被拒绝了）
_ACTIONS = """CREATE_INDEX CREATE_TABLE CREATE_TEMP_INDEX CREATE_TEMP_TABLE CREATE_TEMP_TRIGGER CREATE_TEMP_VIEW
CREATE_TRIGGER CREATE_VIEW DELETE DROP_INDEX DROP_TABLE DROP_TEMP_INDEX DROP_TEMP_TABLE DROP_TEMP_TRIGGER
DROP_TEMP_VIEW DROP_TRIGGER DROP_VIEW INSERT PRAGMA READ SELECT TRANSACTION UPDATE ATTACH DETACH ALTER_TABLE
REINDEX ANALYZE CREATE_VTABLE DROP_VTABLE FUNCTION SAVEPOINT RECURSIVE""".split()
ACTION_NAMES = {getattr(sqlite3, "SQLITE_" + a): a for a in _ACTIONS if hasattr(sqlite3, "SQLITE_" + a)}


class SQLPolicyError(sqlite3.DatabaseError):
    """违反安全策略的 SQL。继承 sqlite3.Error，所以 run_sql 会把它转成 ToolError 交给模型。"""


def safe_execute(db_path: Path, sql: str, limit: int) -> QueryResult:
    denied: list[str] = []

    def authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None) -> int:
        if action == sqlite3.SQLITE_READ and (arg1, arg2) in MASKED_COLUMNS:
            return sqlite3.SQLITE_IGNORE  # 读这一列时返回 NULL，而不是报错，正常查询不受影响
        if action in ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        denied.append(ACTION_NAMES.get(action, str(action)) + (f" {arg1}" if arg1 else ""))
        return sqlite3.SQLITE_DENY

    deadline = time.monotonic() + TIMEOUT_SECONDS

    def check_timeout() -> int:
        return 1 if time.monotonic() > deadline else 0  # 返回非 0 会中断正在执行的查询

    # as_uri() 会正确处理路径里的空格和中文
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.set_authorizer(authorizer)
    conn.set_progress_handler(check_timeout, 10_000)
    limit = min(limit, MAX_ROWS)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit + 1) if columns else []
    except sqlite3.DatabaseError as e:
        if denied:
            raise SQLPolicyError(f"安全策略拒绝了这条 SQL（只允许只读查询），被拒绝的操作：{', '.join(denied)}") from e
        if "interrupted" in str(e):
            raise SQLPolicyError(f"查询超过 {TIMEOUT_SECONDS} 秒被中断，请缩小范围或加上过滤条件") from e
        raise
    finally:
        conn.close()

    truncated = len(rows) > limit
    return QueryResult(columns=columns, rows=[list(r) for r in rows[:limit]], row_count=min(len(rows), limit), truncated=truncated)
