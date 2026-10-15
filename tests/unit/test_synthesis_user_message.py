"""Synthesis must send a user-role message: qwen-style chat templates reject
a prompt with no user message ("No user query found in messages").

The synthesis prompt used to be system-only, which made every run fail its
synthesis step on Groq/qwen right after the work finished (the apply still
landed, but the wrap-up never streamed and the run looked stuck).
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ops_copilot.graph import supervisor as supervisor_mod
from ops_copilot.graph.state import Task


class _DummyModel:
    pass


async def _capture_ainvoke_patient(model, messages, context=None):
    _capture_ainvoke_patient.captured = messages
    return SimpleNamespace(text="wrap-up", content="wrap-up")


def _state(messages, tasks):
    return {"messages": messages, "tasks": tasks, "awaiting": None, "error": None}


def _task():
    return Task(
        id="t1",
        description="check revenue",
        assignee="analyst",
        status="done",
        summary="revenue 203146.58",
    )


async def test_synthesis_includes_user_query(monkeypatch):
    monkeypatch.setattr(supervisor_mod, "build_chat_model", lambda *a, **k: _DummyModel())
    monkeypatch.setattr(supervisor_mod, "ainvoke_patient", _capture_ainvoke_patient)
    state = _state(
        [HumanMessage(content="what is our revenue?"), AIMessage(content="x")],
        [_task()],
    )
    answer = await supervisor_mod._synthesize(state)
    assert answer == "wrap-up"
    prompt = _capture_ainvoke_patient.captured
    assert any(isinstance(m, SystemMessage) for m in prompt)
    assert isinstance(prompt[-1], HumanMessage)
    assert prompt[-1].content == "what is our revenue?"


async def test_synthesis_falls_back_without_user_message(monkeypatch):
    monkeypatch.setattr(supervisor_mod, "build_chat_model", lambda *a, **k: _DummyModel())
    monkeypatch.setattr(supervisor_mod, "ainvoke_patient", _capture_ainvoke_patient)
    state = _state([AIMessage(content="x")], [_task()])
    await supervisor_mod._synthesize(state)
    prompt = _capture_ainvoke_patient.captured
    assert isinstance(prompt[-1], HumanMessage)
    assert prompt[-1].content == "Summarize the completed work for the user."
