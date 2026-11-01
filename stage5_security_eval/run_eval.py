"""阶段 5：效果评估。用 20 道有标准答案的问题给 Agent 打分。

运行：
    python -m stage5_security_eval.run_eval                 # 评估加固版（servers_secure.json）
    python -m stage5_security_eval.run_eval --naive         # 评估阶段 3 的版本（servers.json）
    python -m stage5_security_eval.run_eval --only 1 5 9    # 只跑部分题目

标准答案不是写死的：每道题带一条 reference_sql，评估时现场在数据库上执行，取结果作为期望值。
评分方式：期望值必须出现在 Agent 的回答里（数字允许 0.1% 的误差，文字忽略空格和书名号）。

⚠️ 没配置 API Key 时用的是 MockLLM，它只认识几种固定问法，分数很低是正常的 ——
   这时评估只能证明"评估流程本身能跑通"。换成真实模型后，这份报告才有意义。
"""

import argparse
import asyncio
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from common.config import DEFAULT_DB
from common.llm import create_llm
from stage3_agent.agent import DEFAULT_CONFIG as NAIVE_CONFIG
from stage3_agent.agent import MCPAgent, load_config
from stage5_security_eval.secure_agent import DEFAULT_CONFIG as SECURE_CONFIG
from stage5_security_eval.secure_agent import SecureMCPAgent

HERE = Path(__file__).parent
QUESTIONS = HERE / "eval_questions.json"
RESULTS = HERE / "eval_results.json"


@dataclass
class EvalRecord:
    id: int
    question: str
    expected: list[Any]
    passed: bool
    steps: int
    tool_calls: int
    tool_errors: int
    seconds: float
    answer: str


def expected_values(item: dict[str, Any], db_path: Path) -> list[Any]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(item["reference_sql"]).fetchall()
    finally:
        conn.close()
    rows = rows if item.get("check_rows") == "all" else rows[:1]
    return [row[0] for row in rows]


def normalize(text: str) -> str:
    return re.sub(r"[\s《》,，]", "", text)


def contains_value(answer: str, value: Any) -> bool:
    if isinstance(value, (int, float)):
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", answer.replace(",", "").replace("，", ""))]
        return any(abs(n - value) <= max(0.011, abs(value) * 0.001) for n in numbers)
    return normalize(str(value)) in normalize(answer)


def grade(item: dict[str, Any], answer: str, expected: list[Any]) -> bool:
    if "expect_any" in item:
        return any(normalize(alt) in normalize(answer) for alt in item["expect_any"])
    return all(contains_value(answer, v) for v in expected)


async def run(agent: MCPAgent, items: list[dict[str, Any]]) -> list[EvalRecord]:
    records = []
    for item in items:
        agent.reset()  # 每道题独立，不带上一题的对话历史
        expected = expected_values(item, DEFAULT_DB)
        start = time.monotonic()
        try:
            result = await agent.ask(item["question"])
            answer, steps, calls = result.answer, result.steps, result.tool_calls
        except Exception as e:  # 模型接口报错等，记为失败但继续评估
            answer, steps, calls = f"【异常】{type(e).__name__}: {e}", 0, []
        record = EvalRecord(
            id=item["id"], question=item["question"], expected=expected,
            passed=grade(item, answer, expected), steps=steps, tool_calls=len(calls),
            tool_errors=sum(c.is_error for c in calls), seconds=round(time.monotonic() - start, 2), answer=answer,
        )
        records.append(record)
        print(f"{'✅' if record.passed else '❌'} Q{record.id:<2} 步数={record.steps} 工具调用={record.tool_calls} "
              f"出错={record.tool_errors}  {record.question}  期望={expected}")
    return records


def summarize(records: list[EvalRecord], model: str) -> dict[str, Any]:
    n = len(records)
    return {
        "model": model,
        "questions": n,
        "accuracy": round(sum(r.passed for r in records) / n, 3),
        "avg_steps": round(sum(r.steps for r in records) / n, 2),
        "avg_tool_calls": round(sum(r.tool_calls for r in records) / n, 2),
        "tool_error_rate": round(sum(r.tool_errors for r in records) / max(1, sum(r.tool_calls for r in records)), 3),
        "avg_seconds": round(sum(r.seconds for r in records) / n, 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--naive", action="store_true", help="评估阶段 3 的朴素版")
    parser.add_argument("--only", type=int, nargs="*", help="只跑这些题号")
    args = parser.parse_args()

    items = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    if args.only:
        items = [q for q in items if q["id"] in args.only]

    llm = create_llm()
    agent_cls, config = (MCPAgent, NAIVE_CONFIG) if args.naive else (SecureMCPAgent, SECURE_CONFIG)
    async with agent_cls(llm, load_config(config), verbose=False) as agent:
        records = await run(agent, items)

    summary = summarize(records, llm.name)
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    RESULTS.write_text(json.dumps({"summary": summary, "records": [asdict(r) for r in records]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"详细结果已保存到 {RESULTS.relative_to(HERE.parent)}")
    if llm.name == "mock":
        print("（当前是 MockLLM，分数只说明评估流程能跑通；配置 .env 里的 LLM_API_KEY 后再评估真实模型。）")


if __name__ == "__main__":
    asyncio.run(main())
