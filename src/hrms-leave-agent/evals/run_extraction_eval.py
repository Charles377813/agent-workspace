"""LLM 抽參數評估：每題只呼叫一次模型、不執行工具、不寫資料庫，比對模型「想呼叫什麼」。

用法（在專案目錄）：.venv/Scripts/python evals/run_extraction_eval.py [--runs 1]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from agent_app import (  # noqa: E402
    DEFAULT_MODEL,
    MAX_TOKENS,
    build_system_prompt,
    fallback_options,
    server_environment,
    to_anthropic_tools,
)
from extraction_cases import CASES, EMPLOYEE, TODAY  # noqa: E402


def judge(case: dict, tool_uses: list) -> tuple[bool, str]:
    calls = [(b.name, b.input) for b in tool_uses]
    kind = case["kind"]
    if kind == "apply":
        applies = [i for n, i in calls if n == "apply_leave"]
        if not applies:
            return False, f"沒呼叫 apply_leave（呼叫：{[n for n, _ in calls]}）"
        got = (applies[0].get("leave_type"), applies[0].get("start_at"), applies[0].get("end_at"))
        return got == case["expect"], f"實際 {got}"
    if kind == "ask":
        return not calls, f"呼叫了 {[n for n, _ in calls]}" if calls else "有反問"
    if kind == "no_apply":
        bad = [n for n, _ in calls if n == "apply_leave"]
        return not bad, "呼叫了 apply_leave" if bad else "沒送出"
    queries = [i for n, i in calls if n == "query_leave_balance"]
    if not queries:
        return False, f"沒呼叫 query_leave_balance（呼叫：{[n for n, _ in calls]}）"
    expected = case["expect_type"]
    got = queries[0].get("leave_type")
    return expected is None or got == expected, f"leave_type={got}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="每題重複次數")
    args = parser.parse_args()

    load_dotenv(PROJECT_DIR / ".env")
    model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    client = anthropic.AsyncAnthropic()
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_DIR / "mcp_server.py")],
        cwd=str(PROJECT_DIR),
        env=server_environment(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = to_anthropic_tools((await session.list_tools()).tools)

    system = build_system_prompt(date.fromisoformat(TODAY), EMPLOYEE)
    print(f"模型 {model}，今天 {TODAY}，每題 {args.runs} 次\n")
    passed = total = 0
    for case in CASES:
        for run in range(args.runs):
            response = await client.beta.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=[{"role": "user", "content": case["text"]}],
                **fallback_options(model),
            )
            ok, detail = judge(case, [b for b in response.content if b.type == "tool_use"])
            total += 1
            passed += ok
            print(f"{'✅' if ok else '❌'} #{case['id']:>2} [{case['kind']}] {case['text']}  →  {detail}")
    print(f"\n結果：{passed}/{total}")


if __name__ == "__main__":
    asyncio.run(main())
