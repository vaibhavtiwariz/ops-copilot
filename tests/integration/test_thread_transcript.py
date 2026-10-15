"""GET /threads/{id}/transcript: token-gated, human + assistant text only.

The console fetches it on thread open (reopening shows the real
conversation) and on done (backstop for WS tokens missed mid-run). Tool
messages and empty assistant turns are omitted; no SQL rides along.
"""

import hashlib
import os

if not os.environ.get("OPS_API_KEY"):
    os.environ["OPS_API_KEY"] = "test-key-not-real"
if not os.environ.get("POSTGRES_PASSWORD"):
    os.environ["POSTGRES_PASSWORD"] = "test-password-not-real"

from typing import Annotated

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

import ops_copilot.api.main as api_main
from ops_copilot.api.main import create_app

API_KEY = os.environ["OPS_API_KEY"]
AUTH_HEADERS = {"X-API-Key": API_KEY}


def thread_headers(thread_token: str) -> dict[str, str]:
    return {**AUTH_HEADERS, "X-Thread-Token": thread_token}


def ws_subprotocols(thread_token: str) -> list[str]:
    return ["copilot", f"x-api-key.{API_KEY}", f"x-thread-token.{thread_token}"]


class StubState(TypedDict):
    messages: Annotated[list, lambda a, b: a + b]


def _stub_graph():
    builder = StateGraph(StubState)

    async def chat(state):
        return {"messages": [AIMessage(content="checking the numbers now")]}

    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.fixture()
def client(monkeypatch):
    issued: dict[str, str] = {}

    async def fake_issue(request, thread_id: str) -> str:
        token = f"test-token-for-{thread_id}"
        issued[thread_id] = hashlib.sha256(token.encode()).hexdigest()
        return token

    async def fake_lookup(pool, thread_id: str) -> str:
        return issued.get(thread_id, "")

    monkeypatch.setattr(api_main, "_issue_thread_token", fake_issue)
    monkeypatch.setattr(api_main, "_lookup_token_hash", fake_lookup)

    app = create_app(graph=_stub_graph())
    with TestClient(app) as test_client:
        yield test_client


def make_thread(client) -> tuple[str, str]:
    response = client.post("/api/threads", headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    return body["thread_id"], body["thread_token"]


def test_transcript_returns_conversation(client):
    thread, token = make_thread(client)
    with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "content": "what is revenue?"})
        for _ in range(20):
            frame = ws.receive_json()
            if frame.get("type") == "done":
                break
        else:
            raise AssertionError("run never reached done")

    res = client.get(f"/api/threads/{thread}/transcript", headers=thread_headers(token))
    assert res.status_code == 200
    messages = res.json()
    assert {"role": "user", "content": "what is revenue?"} in messages
    assert {"role": "assistant", "content": "checking the numbers now"} in messages


def test_transcript_empty_for_fresh_thread(client):
    thread, token = make_thread(client)
    res = client.get(f"/api/threads/{thread}/transcript", headers=thread_headers(token))
    assert res.status_code == 200
    assert res.json() == []


def test_transcript_requires_thread_token(client):
    thread, _token = make_thread(client)
    denied = client.get(f"/api/threads/{thread}/transcript", headers=AUTH_HEADERS)
    assert denied.status_code == 403
    wrong = client.get(f"/api/threads/{thread}/transcript", headers=thread_headers("wrong-token"))
    assert wrong.status_code == 403
