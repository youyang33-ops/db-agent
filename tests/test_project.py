"""整个项目的自动化测试：python -m pytest -q

每个测试都用临时目录里的数据库副本，不会修改 data/shop.db。
测试本身也是学习材料：它展示了怎么用"进程内 Client"（Client(server 实例)）测试 MCP server，不需要起子进程或端口。
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx2
import pytest

from common.config import PROJECT_ROOT
from common.llm import MockLLM, OpenAICompatibleLLM
from data.init_db import build
from mcp import Client, StdioServerParameters
from mcp.types import ElicitResult, TextContent


def run(coro):
    return asyncio.run(coro)


def text(result) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.db"
    build(path)
    return path


# ------------------------------------------------------------------ 阶段 1
def test_stage1_loop_with_mock(monkeypatch, db):
    from stage1_function_calling import agent as stage1

    monkeypatch.setattr(stage1, "DEFAULT_DB", db)
    messages = [{"role": "system", "content": stage1.SYSTEM_PROMPT}, {"role": "user", "content": "一共有多少个客户？"}]
    answer = run(stage1.run_agent(MockLLM(), messages, verbose=False))
    assert "80" in answer
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]


def test_stage1_tool_error_goes_back_to_model(monkeypatch, db):
    from stage1_function_calling import agent as stage1

    monkeypatch.setattr(stage1, "DEFAULT_DB", db)
    assert stage1.execute_tool("run_sql", json.dumps({"sql": "SELECT nope FROM products"})).startswith("错误")


# ------------------------------------------------------------------ 阶段 2
def test_stage2_primitives(db):
    from stage2_mcp_server.server import build_server

    async def main():
        async with Client(build_server(db_path=db)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"list_tables", "describe_table", "run_sql"}
            assert tools["run_sql"].input_schema["required"] == ["sql"]  # limit 有默认值，不是必填
            assert tools["run_sql"].output_schema is not None

            ok = await client.call_tool("run_sql", {"sql": "SELECT COUNT(*) AS n FROM customers"})
            assert ok.structured_content["rows"] == [[80]]

            bad = await client.call_tool("run_sql", {"sql": "SELECT nope FROM products"})
            assert bad.is_error and "describe_table" in text(bad)

            missing = await client.call_tool("describe_table", {"table": "nope"})
            assert missing.is_error and "orders" in text(missing)

            schema = await client.read_resource("schema://tables/orders")
            assert "status" in schema.contents[0].text

            prompt = await client.get_prompt("analyze_sales", {"month": "2026-08"})
            assert "2026-08" in prompt.messages[0].content.text

    run(main())


# ------------------------------------------------------------------ 阶段 3
def test_stage3_agent_with_two_stdio_servers(tmp_path, db):
    """真正启动两个 stdio 子进程。注意 env：子进程不会继承父进程的环境变量，要显式传。"""
    from stage3_agent.agent import MCPAgent, ServerTarget

    env = {"SHOP_DB": str(db), "REPORT_DIR": str(tmp_path / "reports")}

    def stdio(module: str) -> StdioServerParameters:
        return StdioServerParameters(command=sys.executable, args=["-m", module], env=env, cwd=str(PROJECT_ROOT))

    servers = {
        "shop_db": ServerTarget(stdio("stage2_mcp_server.server"), ["schema://tables"]),
        "reports": ServerTarget(stdio("stage3_agent.report_server")),
    }

    async def main():
        async with MCPAgent(MockLLM(), servers, verbose=False) as agent:
            assert "shop_db__run_sql" in agent.tool_index and "reports__save_report" in agent.tool_index
            assert "CREATE TABLE orders" in agent.system_prompt  # Host 把资源读进了系统提示词
            result = await agent.ask("销量最高的 3 个商品是什么？保存成报告")
            assert [c.tool for c in result.tool_calls] == ["shop_db__run_sql", "reports__save_report"]
            assert "零糖 苏打水 24 瓶" in result.answer

            bad = await agent.call_tool("shop_db__run_sql", '{"sql": "SELEC 1"}')
            assert bad.is_error and bad.result.startswith("【工具执行出错】")
            assert (await agent.call_tool("nope__tool", "{}")).is_error
            assert (await agent.call_tool("shop_db__run_sql", "not json")).is_error

            assert "2026-08" in await agent.render_prompt("analyze_sales", {"month": "2026-08"})

    run(main())
    assert (tmp_path / "reports" / "report.md").exists()


def test_openai_compatible_wrapper_against_fake_api():
    """不花钱验证真实模型的封装：起一个假的 OpenAI 兼容接口，返回一个 tool_call。"""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            body = json.dumps({
                "id": "x", "object": "chat.completion", "created": 0, "model": "fake",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "run_sql", "arguments": "{\"sql\": \"SELECT 1\"}"}}]}}],
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        llm = OpenAICompatibleLLM(f"http://127.0.0.1:{server.server_port}/v1", "sk-test", "fake-model")
        tools = [{"type": "function", "function": {"name": "run_sql", "parameters": {"type": "object"}}}]
        reply = run(llm.chat([{"role": "user", "content": "hi"}], tools))
    finally:
        server.shutdown()
    assert seen["body"]["model"] == "fake-model" and seen["body"]["tools"] == tools
    assert reply == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "run_sql", "arguments": "{\"sql\": \"SELECT 1\"}"}}]}


# ------------------------------------------------------------------ 阶段 4
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def http_server(db):
    port = free_port()
    env = {**os.environ, "SHOP_DB": str(db), "MCP_PORT": str(port), "MCP_API_TOKEN": "test-token",
           "MCP_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    proc = subprocess.Popen([sys.executable, "-m", "stage4_remote.server_http"], env=env, cwd=PROJECT_ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(50):
        try:
            httpx2.get(f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp", timeout=0.5)
            break
        except httpx2.HTTPError:
            time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def test_stage4_auth_progress_and_both_protocol_eras(http_server):
    from mcp.client.streamable_http import streamable_http_client

    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    assert httpx2.post(http_server, json=ping, headers=headers).status_code == 401
    assert httpx2.post(http_server, json=ping, headers={**headers, "authorization": "Bearer wrong"}).status_code == 401

    progress = []

    async def on_progress(p, total, message):
        progress.append((p, total))

    async def main():
        for mode, version in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer test-token"},
                                          timeout=httpx2.Timeout(30, read=300)) as http:
                async with Client(streamable_http_client(http_server, http_client=http), mode=mode) as client:
                    assert client.protocol_version == version
                    who = await client.call_tool("whoami", {})
                    assert who.structured_content["subject"] == "analyst"
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer test-token"},
                                      timeout=httpx2.Timeout(30, read=300)) as http:
            async with Client(streamable_http_client(http_server, http_client=http)) as client:
                report = await client.call_tool("monthly_sales_report", {"months": ["2026-07", "2026-08"]},
                                                progress_callback=on_progress)
                assert report.structured_content["rows"][1]["revenue"] == 154950.0

    run(main())
    assert progress == [(1, 2), (2, 2)]


# ------------------------------------------------------------------ 阶段 5
def test_sql_guard(db):
    from stage5_security_eval.sql_guard import SQLPolicyError, safe_execute

    assert safe_execute(db, "SELECT phone, city FROM customers LIMIT 1", 5).rows[0][0] is None  # 脱敏
    assert safe_execute(db, "SELECT * FROM orders", 10_000).row_count == 200  # 行数上限
    for sql in ["DROP TABLE orders", "DELETE FROM orders", "UPDATE products SET price = 1",
                "INSERT INTO categories VALUES (99, 'x')", "PRAGMA table_info(orders)", "ATTACH 'x.db' AS x"]:
        with pytest.raises(SQLPolicyError):
            safe_execute(db, sql, 5)
    with pytest.raises(SQLPolicyError, match="中断"):
        safe_execute(db, "SELECT COUNT(*) FROM order_items a, order_items b, order_items c", 5)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_price_update_requires_user_confirmation(tmp_path, db, mode):
    from stage5_security_eval.server_secure import build_secure_server

    server = build_secure_server(db, log_dir=tmp_path / "logs")

    def answer(confirm: bool):
        async def callback(context, params):
            assert "星云 X5 手机" in params.message
            return ElicitResult(action="accept", content={"confirm": confirm})
        return callback

    async def main():
        async with Client(server, mode=mode, elicitation_callback=answer(False)) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == "update_product_price")
            assert "confirmation" not in tool.input_schema["properties"]  # 模型无法替用户回答
            assert "未修改" in text(await client.call_tool("update_product_price", {"product_id": 1, "new_price": 1999}))
        async with Client(server, mode=mode, elicitation_callback=answer(True)) as client:
            assert "已把" in text(await client.call_tool("update_product_price", {"product_id": 1, "new_price": 1999}))
            price = await client.call_tool("run_sql", {"sql": "SELECT price FROM products WHERE id = 1"})
            assert price.structured_content["rows"] == [[1999.0]]

    run(main())
    assert "update_product_price 已修改" in (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8")


def test_injection_demo_naive_falls_secure_holds(tmp_path):
    from stage2_mcp_server.server import build_server
    from stage3_agent.agent import MCPAgent
    from stage5_security_eval import injection_demo as demo
    from stage5_security_eval.secure_agent import SecureMCPAgent
    from stage5_security_eval.server_secure import build_secure_server

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    naive_db, secure_db = demo.make_db_copy(tmp_path / "a"), demo.make_db_copy(tmp_path / "b")
    assert run(demo.prompt_injection(MCPAgent, build_server(db_path=naive_db), naive_db)) is False
    assert run(demo.prompt_injection(SecureMCPAgent, build_secure_server(secure_db, tmp_path / "logs"), secure_db)) is True

    reports = tmp_path / "r" / "reports"
    assert run(demo.path_traversal(demo.build_naive_reports(reports), reports)) is True
    reports2 = tmp_path / "r2" / "reports"
    assert run(demo.path_traversal(demo.build_secure_reports(reports2), reports2)) is False


def test_eval_grading():
    from stage5_security_eval.run_eval import grade

    assert grade({}, "8 月销售额为 154,950 元", [154950.0])
    assert grade({}, "约 154950.00 元", [154950.0])
    assert not grade({}, "约 15 万元", [154950.0])
    assert grade({}, "最多的是疾风跑步鞋", ["疾风 跑步鞋"])
    assert grade({}, "是三体全集", ["《三体》全集"])
    assert grade({"expect_any": ["2026-08", "8月"]}, "销售额最高的是 2026 年 8 月", ["2026-08"])
    assert not grade({}, "一共 80 个", [16])
