"""The WS idle timeout must not kill a socket while a run is in flight.

An approval resume is driven by the HTTP endpoint, not by the socket, so
the socket sits in receive_json while the resume runs. If the idle timeout
fires mid-resume it closes the socket and the resume's done event never
arrives. A truly idle socket (no run) must still close.

The resume test POSTs from a separate thread so the main thread keeps
reading the socket: in the test client, the server-side close() only
completes once the test side reads the close frame, so a blocked POST
would otherwise mask the close.
"""

import asyncio
import hashlib
import os
import threading

# The API boots fail-closed without OPS_API_KEY and POSTGRES_PASSWORD. Set
# throwaway test values here (not only in tests/conftest.py) so this module
# also passes when conftest is bypassed by an external runner.
if not os.environ.get("OPS_API_KEY"):
    os.environ["OPS_API_KEY"] = "test-key-not-real"
if not os.environ.get("POSTGRES_PASSWORD"):
    os.environ["POSTGRES_PASSWORD"] = "test-password-not-real"

from typing import Annotated

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
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


def _slow_graph():
    builder = StateGraph(StubState)

    async def chat(state):
        return {"messages": [AIMessage(content="checking")]}

    async def gate(state):
        decision = interrupt(
            {
                "kind": "github_comment",
                "repo": "acme/widgets",
                "number": 12,
                "body": "thanks for the report, we are looking into it",
                "summary": "Draft comment for acme/widgets#12",
            }
        )
        note = "approved" if decision.get("approved") else "declined"
        return {"messages": [AIMessage(content=f"review outcome: {note}")]}

    async def stop(state):
        # The resume path takes longer than the patched idle timeout, so an
        # unfixed socket would be closed mid-resume and never see done.
        await asyncio.sleep(2)
        return {}

    builder.add_node("chat", chat)
    builder.add_node("gate", gate)
    builder.add_node("stop", stop)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", "gate")
    builder.add_edge("gate", "stop")
    builder.add_edge("stop", END)
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
    monkeypatch.setattr(api_main, "WS_IDLE_TIMEOUT_S", 1)

    app = create_app(graph=_slow_graph())
    with TestClient(app) as test_client:
        yield test_client


def make_thread(client) -> tuple[str, str]:
    response = client.post("/api/threads", headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    return body["thread_id"], body["thread_token"]


def read_until(socket, wanted_types: set[str], max_frames: int = 60):
    for _ in range(max_frames):
        frame = socket.receive_json()
        if frame.get("type") in wanted_types:
            return frame
    raise AssertionError(f"never saw {wanted_types}")


class TestWsIdleTimeout:
    def test_socket_survives_idle_timeout_during_resume(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "user_message", "content": "draft the write"})
            read_until(ws, {"interrupt", "done"})

            result: dict = {}

            def do_post():
                result["resp"] = client.post(
                    f"/api/threads/{thread}/approvals",
                    json={"approved": True, "note": "ship it", "decided_by": "reviewer-1"},
                    headers=thread_headers(token),
                )

            poster = threading.Thread(target=do_post)
            poster.start()
            try:
                # The resume takes 2s, the idle timeout is 1s: an unfixed
                # socket is closed here with 4408 and done never arrives.
                frame = read_until(ws, {"done"}, max_frames=80)
                assert frame["type"] == "done"
            finally:
                poster.join(timeout=10)
            assert result["resp"].status_code == 200

    def test_truly_idle_socket_still_closes(self, client):
        thread, token = make_thread(client)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws,
        ):
            assert ws.receive_json()["type"] == "connected"
            ws.receive_json()  # blocks until the server closes the idle socket
        assert exc_info.value.code == 4408
