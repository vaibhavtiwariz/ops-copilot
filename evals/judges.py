"""Live LLM-as-judge scoring for answer quality.

Deliberately separate from runner.py: the golden trajectories prove *behavior*
(routing, tool choice, gating) deterministically and run everywhere, including
CI without credentials. This module asks a real model whether a produced
answer actually addresses the request - useful locally when a free-tier key is
available, skipped silently otherwise so it never blocks anyone.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

RUBRIC = """You are grading an AI operations assistant. Given a user request and
the assistant's final answer, score 1-5 on:
- groundedness: does every claim trace to tool output in the transcript?
- completeness: does it address what was asked?
- tone: concise, professional, no hedging filler.

Respond with JSON: {"score": <1-5>, "reason": "<one sentence>"}"""


async def judge_case(case_input: str, final_answer: str) -> dict | None:
    if os.environ.get("OPS_EVAL_OFFLINE") == "1":
        # evals/runner.py sets this alongside its dummy key; the dummy is not
        # a credential, so it must not switch the live judge on.
        return None
    if not os.environ.get("OPS_GROQ_API_KEY") and not os.environ.get("OPS_GEMINI_API_KEY"):
        return None
    try:
        from ops_copilot.llm import build_chat_model
        from ops_copilot.settings import get_settings

        settings = get_settings()
        model = build_chat_model(settings, temperature=0.0)
        response = await model.ainvoke(
            [
                ("system", RUBRIC),
                ("human", f"Request:\n{case_input}\n\nAnswer:\n{final_answer}"),
            ]
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start : end + 1])
    except Exception as exc:
        print(f"judge unavailable: {exc}", file=sys.stderr)
        return None


async def judge_all() -> None:
    from evals.runner import DATASET, load_cases  # reuse dataset inputs

    del DATASET
    cases = load_cases()
    scored = []
    for case in cases:
        verdict = await judge_case(case["input"], case["plan"].get("reply", "(delegated flow)"))
        if verdict:
            scored.append((case["id"], verdict))
            print(f"{case['id']}: {verdict['score']} - {verdict['reason']}")
    if not scored:
        print("no live judge available; golden-trajectory evals are the gate")


if __name__ == "__main__":
    asyncio.run(judge_all())
