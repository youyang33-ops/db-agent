"""阶段 5：加固版 shop-db server。

和阶段 2 的区别（工具代码复用 build_server，没有复制）：
1. run_sql 换成 sql_guard.safe_execute（只读连接 + authorizer + 脱敏 + 超时）；
2. 每条 SQL 都写审计日志 logs/audit.log；
3. 新增一个"写操作"工具 update_product_price：执行前必须由用户确认（Elicitation），
   确认这一步由 SDK 的 Elicit 依赖完成，模型无法替用户回答。

运行：python -m stage5_security_eval.server_secure（stdio）
"""

import logging
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from common.config import DEFAULT_DB, LOG_DIR
from common.db import QueryResult
from mcp.server.mcpserver import Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from stage2_mcp_server.server import build_server
from stage5_security_eval.sql_guard import safe_execute


def get_audit_logger(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(f"audit.{log_dir.resolve()}")  # 每个日志目录一个 logger，测试时互不干扰
    if not logger.handlers:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "audit.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class PriceChange(BaseModel):
    confirm: bool = Field(description="确认修改价格吗？")


def build_secure_server(db_path: Path = DEFAULT_DB, log_dir: Path = LOG_DIR) -> MCPServer:
    audit = get_audit_logger(log_dir)

    def audited_execute(path: Path, sql: str, limit: int) -> QueryResult:
        try:
            result = safe_execute(path, sql, limit)
        except sqlite3.Error as e:
            audit.warning("run_sql 拒绝/失败 sql=%r error=%s", sql, e)
            raise
        audit.info("run_sql ok rows=%d sql=%r", result.row_count, sql)
        return result

    mcp = build_server(name="shop-db-secure", db_path=db_path, executor=audited_execute)

    def lookup_product(product_id: int) -> tuple[str, float]:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT name, price FROM products WHERE id = ?", (product_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ToolError(f"商品 {product_id} 不存在")
        return row

    async def confirm_price_change(product_id: int, new_price: float) -> Elicit[PriceChange]:
        # 解析器（resolver）：在工具执行之前运行。返回 Elicit 表示"去问用户"，
        # SDK 会根据协议版本自动选择提问方式（2026-07-28：多轮往返；旧版：server 主动发 elicitation 请求）。
        name, old_price = lookup_product(product_id)
        return Elicit(f"确认把「{name}」的价格从 {old_price} 元改成 {new_price} 元吗？", PriceChange)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    def update_product_price(
        product_id: int = Field(description="商品 id"),
        new_price: float = Field(gt=0, le=100_000, description="新价格（元）"),
        confirmation: Annotated[PriceChange, Resolve(confirm_price_change)] = None,  # type: ignore[assignment]
    ) -> str:
        """修改商品价格。这是写操作，执行前会请用户确认。"""
        # confirmation 不在 input_schema 里，模型传不了，只能来自用户的回答
        if not confirmation.confirm:
            audit.info("update_product_price 用户拒绝 product_id=%s new_price=%s", product_id, new_price)
            return "用户没有确认，价格未修改。"
        name, old_price = lookup_product(product_id)
        conn = sqlite3.connect(db_path)  # 写操作用单独的连接，并且只执行这一条参数化语句
        try:
            conn.execute("UPDATE products SET price = ? WHERE id = ?", (new_price, product_id))
            conn.commit()
        finally:
            conn.close()
        audit.info("update_product_price 已修改 product_id=%s %s -> %s", product_id, old_price, new_price)
        return f"已把「{name}」的价格从 {old_price} 元改为 {new_price} 元。"

    return mcp


mcp = build_secure_server()

if __name__ == "__main__":
    mcp.run()
