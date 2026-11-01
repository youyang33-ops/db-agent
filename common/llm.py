"""大模型客户端。

所有阶段都通过 `await llm.chat(messages, tools)` 调用模型，消息和工具都用 OpenAI 的 Chat Completions 格式：
DeepSeek、通义千问、Kimi、智谱等国内模型都提供这个格式的兼容接口，换模型只需要改 .env。

没有配置 API Key 时自动使用 MockLLM：一个按关键词匹配的"假模型"，
只用来在离线状态下验证整条链路（模型 → 工具调用 → MCP → 数据库 → 回答）是否跑通。
"""

import json
import re
from typing import Any, Protocol

from openai import AsyncOpenAI

from common.config import env

Message = dict[str, Any]
ToolSpec = dict[str, Any]


class LLM(Protocol):
    name: str

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        """输入对话和可用工具，返回一条 assistant 消息（可能带 tool_calls）。"""
        ...


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.name = model

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
        response = await self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        # 只保留下一轮需要回传给模型的字段。注意：带 tool_calls 的 assistant 消息必须原样放回历史，
        # 否则下一轮模型看到 tool 消息却找不到对应的调用，接口会报错。
        out: Message = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            out["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return out


# ---------------------------------------------------------------------------
# MockLLM：离线规则模拟
# ---------------------------------------------------------------------------

# 每条规则：(关键词组, SQL)。每个关键词组里任意一个词出现即算命中，所有组都命中才使用这条规则。
MOCK_RULES: list[tuple[list[tuple[str, ...]], str]] = [
    (
        [("销量",), ("最高", "前三", "前 3", "排行")],
        "SELECT p.name AS 商品, SUM(oi.quantity) AS 销量 FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id JOIN products p ON p.id = oi.product_id "
        "WHERE o.status = '已完成' GROUP BY p.id ORDER BY 销量 DESC LIMIT 3",
    ),
    (
        [("客户", "用户"), ("多少", "几个", "总数")],
        "SELECT COUNT(*) AS 客户数 FROM customers",
    ),
    (
        [("城市",), ("客户", "用户")],
        "SELECT city AS 城市, COUNT(*) AS 客户数 FROM customers GROUP BY city ORDER BY 客户数 DESC",
    ),
    (
        [("销售额", "营收", "收入"), ("每月", "每个月", "按月")],
        "SELECT substr(o.order_date, 1, 7) AS 月份, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS 销售额 "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.status = '已完成' "
        "GROUP BY 月份 ORDER BY 月份",
    ),
    (
        [("评价", "评论")],
        "SELECT p.name AS 商品, r.rating AS 星级, r.content AS 内容 FROM reviews r "
        "JOIN products p ON p.id = r.product_id ORDER BY r.created_at DESC, r.id DESC LIMIT 5",
    ),
    (
        [("库存",), ("最少", "不足", "最低")],
        "SELECT name AS 商品, stock AS 库存 FROM products ORDER BY stock ASC LIMIT 5",
    ),
]


def find_tool(names: list[str], suffix: str) -> str | None:
    """在工具列表里找名字以 suffix 结尾的工具（阶段 3 的工具名会带 server 前缀，如 shop_db__run_sql）。"""
    return next((n for n in names if n == suffix or n.endswith("__" + suffix)), None)


def current_turn(messages: list[Message]) -> tuple[str, list[Message]]:
    """返回最后一条用户消息，以及它之后的所有消息（即"这一轮"已经发生的工具调用）。"""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            return str(messages[i]["content"]), messages[i + 1 :]
    return "", []


class MockLLM:
    name = "mock"

    def __init__(self) -> None:
        self._n = 0

    def _call(self, tool: str, args: dict[str, Any]) -> Message:
        self._n += 1
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": f"call_{self._n}", "type": "function",
                 "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}
            ],
        }

    @staticmethod
    def _answer(text: str) -> Message:
        return {"role": "assistant", "content": text}

    @staticmethod
    def match_sql(question: str) -> str | None:
        for groups, sql in MOCK_RULES:
            if all(any(word in question for word in group) for group in groups):
                return sql
        return None

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        names = [t["function"]["name"] for t in tools]
        question, turn = current_turn(messages)
        results = [m for m in turn if m["role"] == "tool"]
        called = [tc["function"]["name"] for m in turn if m["role"] == "assistant" for tc in m.get("tool_calls") or []]

        if not results:
            price = re.search(r"(\d+)\s*号商品的价格改[成为]\s*(\d+(?:\.\d+)?)", question)
            if price and (update := find_tool(names, "update_product_price")):
                return self._call(update, {"product_id": int(price[1]), "new_price": float(price[2])})
            sql = self.match_sql(question)
            if sql and (run_sql := find_tool(names, "run_sql")):
                return self._call(run_sql, {"sql": sql})
            if lt := find_tool(names, "list_tables"):
                return self._call(lt, {})
            return self._answer("（MockLLM）没有可用的工具，我答不了这个问题。")

        first = results[0]["content"]
        if re.search("保存|报告", question) and (save := find_tool(names, "save_report")) and save not in called:
            return self._call(save, {"filename": "report.md", "content": f"# {question}\n\n{first}\n"})

        text = f"（MockLLM 规则模拟，不是真实模型）根据工具返回的结果：\n{first}"
        if len(results) > 1:
            text += f"\n\n{results[-1]['content']}"
        return self._answer(text)


def create_llm() -> LLM:
    if env("LLM_PROVIDER").lower() == "mock" or not env("LLM_API_KEY"):
        return MockLLM()
    return OpenAICompatibleLLM(
        base_url=env("LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=env("LLM_API_KEY"),
        model=env("LLM_MODEL", "deepseek-chat"),
    )
