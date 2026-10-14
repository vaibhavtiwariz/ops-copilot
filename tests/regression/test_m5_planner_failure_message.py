"""Regression tests for M5: a planner failure must surface in chat, not vanish.

On the original code the planner's except path logs a warning and returns
Command(goto="finalize") with no state update, so the run ends "done" with
nothing in chat (a spinner followed by silence when the provider is down).
The fixed behavior appends an explanatory message to state so the user sees
what happened.

The LLM call is monkeypatched to raise; no provider, network, or database is
involved.
"""

import asyncio

from langchain_core.messages import HumanMessage

from ops_copilot.graph import supervisor


def _state():
    return {
        "messages": [HumanMessage(content="summarize open incidents")],
        "tasks": [],
        "awaiting": None,
        "error": None,
    }


async def _boom(state):
    raise RuntimeError("provider exploded")


def test_planner_failure_adds_explanatory_message(monkeypatch):
    monkeypatch.setattr(supervisor, "_make_plan", _boom)
    cmd = asyncio.run(supervisor.planner(_state()))

    assert cmd.goto == "finalize"
    updates = cmd.update or {}
    messages = updates.get("messages") or []
    assert messages, "planner must explain the failure in chat instead of finalizing silently"
    text = " ".join(str(getattr(m, "content", "") or "") for m in messages)
    assert text.strip(), "the explanatory message must have visible content"


def test_planner_failure_message_mentions_the_problem(monkeypatch):
    monkeypatch.setattr(supervisor, "_make_plan", _boom)
    cmd = asyncio.run(supervisor.planner(_state()))
    updates = cmd.update or {}
    messages = updates.get("messages") or []
    assert messages, "planner must explain the failure in chat instead of finalizing silently"
    text = " ".join(str(getattr(m, "content", "") or "") for m in messages).lower()
    assert any(
        word in text for word in ("plan", "fail", "error", "sorry", "could not", "couldn't")
    ), f"message should describe the planning failure, got: {text!r}"
