"""阶段 4：把 shop-db 部署成远程服务（Streamable HTTP + Bearer Token 鉴权 + 进度通知）。

运行：
    python -m stage4_remote.server_http                    # 监听 http://127.0.0.1:8000/mcp
    MCP_STATELESS=1 python -m stage4_remote.server_http    # 无状态模式（只影响旧版协议的客户端，见 README）
    docker build -f stage4_remote/Dockerfile -t shop-db-mcp . && docker run -p 8000:8000 shop-db-mcp

然后在另一个终端运行：python -m stage4_remote.client_remote

和阶段 2 相比，工具代码完全没变（复用 build_server），只多了三件事：
1. 传输方式从 stdio 换成 streamable-http；
2. 传入 token_verifier + auth，SDK 自动拦截没有合法 token 的请求（401）；
3. 新增一个耗时工具，用 ctx.report_progress 汇报进度。
"""

import asyncio
import hmac
import logging
import sqlite3
import time

from pydantic import AnyHttpUrl, BaseModel, Field

from common.config import DEFAULT_DB, env
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from stage2_mcp_server.server import build_server

logger = logging.getLogger("shop-db-remote")

HOST = env("MCP_HOST", "127.0.0.1")
PORT = int(env("MCP_PORT", "8000"))
PUBLIC_URL = env("MCP_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
RESOURCE_URL = f"{PUBLIC_URL}/mcp"
REQUIRED_SCOPE = "shop:read"

MONTHLY_SQL = """
SELECT COUNT(DISTINCT o.id), COALESCE(ROUND(SUM(oi.quantity * oi.unit_price), 2), 0)
FROM orders o JOIN order_items oi ON oi.order_id = o.id
WHERE o.status = '已完成' AND substr(o.order_date, 1, 7) = ?
"""


class StaticTokenVerifier(TokenVerifier):
    """校验请求头里的 Bearer Token。

    演示用：token 来自环境变量 MCP_API_TOKEN。真实项目里这里应该校验 JWT 签名，
    或者调用授权服务器的 introspection 接口（OAuth 2.1），并按 token 返回不同用户和权限范围（scopes）。
    """

    def __init__(self, token: str) -> None:
        self.token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare_digest：比较耗时与内容无关，防止时序攻击
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            return None
        return AccessToken(
            token=token,
            client_id="db-agent",
            subject="analyst",
            scopes=[REQUIRED_SCOPE],
            expires_at=int(time.time()) + 3600,
            resource=RESOURCE_URL,
        )


class MonthlyRow(BaseModel):
    month: str
    orders: int
    revenue: float


class SalesReport(BaseModel):
    rows: list[MonthlyRow]
    total_revenue: float


def build_remote_server(token: str):
    mcp = build_server(
        name="shop-db-remote",
        token_verifier=StaticTokenVerifier(token),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("https://auth.example.com"),  # 授权服务器地址（演示用，没有真的部署）
            resource_server_url=AnyHttpUrl(RESOURCE_URL),
            required_scopes=[REQUIRED_SCOPE],
            validate_token_resource=True,  # token 必须是签发给本服务的，防止拿别的服务的 token 来用
        ),
    )

    @mcp.tool()
    def whoami() -> dict[str, str | list[str]]:
        """返回当前调用者的身份（来自 token）。"""
        token = get_access_token()  # 鉴权通过后，SDK 把校验结果放在这里
        if token is None:  # stdio 下没有 HTTP 请求头，也就没有鉴权
            return {"subject": "anonymous", "client_id": "", "scopes": []}
        return {"subject": token.subject or "", "client_id": token.client_id, "scopes": token.scopes}

    @mcp.tool()
    async def monthly_sales_report(
        ctx: Context,  # Context 参数由 SDK 注入，不会出现在工具的 input_schema 里
        months: list[str] = Field(description="要统计的月份列表，格式 YYYY-MM，例如 ['2026-07', '2026-08']"),
    ) -> SalesReport:
        """逐月统计订单数和销售额（只统计已完成订单）。月份多时比较慢，会实时汇报进度。"""
        rows = []
        conn = sqlite3.connect(DEFAULT_DB)
        try:
            for i, month in enumerate(months, start=1):
                # month 来自模型，必须用 ? 参数化，不能拼进 SQL 字符串（否则就是 SQL 注入）
                orders, revenue = conn.execute(MONTHLY_SQL, (month,)).fetchone()
                rows.append(MonthlyRow(month=month, orders=orders, revenue=revenue))
                await asyncio.sleep(0.3)  # 模拟耗时的计算
                # 进度通知：客户端在 call_tool 时传了 progress_callback 才会收到
                await ctx.report_progress(i, total=len(months), message=f"已完成 {month}")
        finally:
            conn.close()
        return SalesReport(rows=rows, total_revenue=round(sum(r.revenue for r in rows), 2))

    return mcp


def main() -> None:
    token = env("MCP_API_TOKEN", "dev-token-change-me")
    if token == "dev-token-change-me":
        logger.warning("正在使用默认 token，部署前请设置环境变量 MCP_API_TOKEN")
    mcp = build_remote_server(token)

    # DNS 重绑定防护：只接受 Host 头在白名单里的请求。部署到真实域名时要把域名加进来，否则所有请求都返回 421。
    allowed_hosts = env("MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*").split(",")
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        stateless_http=env("MCP_STATELESS") in ("1", "true"),
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts),
    )


if __name__ == "__main__":
    main()
