"""阶段 4：连接远程 MCP server，依次演示鉴权、进度通知、新旧两种协议。

先启动 server：python -m stage4_remote.server_http
再运行：      python -m stage4_remote.client_remote [--url http://127.0.0.1:8000/mcp]
"""

import argparse
import asyncio
import json

import httpx2

from common.config import env
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TIMEOUT = httpx2.Timeout(30.0, read=300.0)


def title(text: str) -> None:
    print(f"\n===== {text} =====")


async def raw_post(url: str, payload: dict, token: str | None = None, protocol: str | None = None) -> httpx2.Response:
    """不用 SDK，直接发一个 HTTP 请求，看清楚 MCP over HTTP 在线路上长什么样。"""
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if protocol:
        headers["mcp-protocol-version"] = protocol
    async with httpx2.AsyncClient(timeout=TIMEOUT) as http:
        return await http.post(url, headers=headers, json=payload)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=env("MCP_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/") + "/mcp")
    args = parser.parse_args()
    url, token = args.url, env("MCP_API_TOKEN", "dev-token-change-me")
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    # ---------------------------------------------------------------- 1. 鉴权
    title("1. 不带 token 访问")
    resp = await raw_post(url, ping)
    print("状态码：", resp.status_code)
    print("WWW-Authenticate：", resp.headers.get("www-authenticate"))
    print("（客户端可以顺着 resource_metadata 找到授权服务器，走 OAuth 流程拿 token）")

    title("2. 带错误的 token 访问")
    print("状态码：", (await raw_post(url, ping, token="wrong-token")).status_code)

    # ------------------------------------------------- 3. 用 SDK 正常连接（新版协议）
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            title("3. 用正确的 token 连接")
            print("协商的协议版本：", client.protocol_version)
            who = await client.call_tool("whoami", {})
            print("whoami：", who.structured_content)

            title("4. 进度通知")

            async def on_progress(progress: float, total: float | None, message: str | None) -> None:
                bar = "█" * int(progress) + "░" * int((total or progress) - progress)
                print(f"  进度 {bar} {progress:.0f}/{total:.0f}  {message}")

            months = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
            report = await client.call_tool("monthly_sales_report", {"months": months}, progress_callback=on_progress)
            print("结果：", json.dumps(report.structured_content, ensure_ascii=False))

    # ------------------------------------------------- 5. 旧版协议（2025-11-25 及以前）
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT) as http:
        async with Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
            title("5. 旧版协议客户端（mode='legacy'，走 initialize 握手）")
            print("协商的协议版本：", client.protocol_version)

    title("6. 会话：旧版 initialize 请求的响应头")
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "raw-demo", "version": "0"}},
    }
    resp = await raw_post(url, init, token=token)
    print("Mcp-Session-Id：", resp.headers.get("mcp-session-id") or "（没有，说明 server 是无状态模式 MCP_STATELESS=1）")
    print("说明：旧版协议用 Mcp-Session-Id 标识会话，多实例部署时需要会话粘性；2026-07-28 版协议去掉了会话，每个请求独立。")


if __name__ == "__main__":
    asyncio.run(main())
