"""API tests against a stub graph: streaming protocol, interrupts, resume."""

import hashlib
import os

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

# The API requires a key (fail-closed auth); the console offers it as a
# WebSocket subprotocol token since browsers cannot set handshake headers.
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
        # A real model would stream; one full message still surfaces as a
        # token event through the messages stream mode.
        return {"messages": [AIMessage(content="checking the numbers now")]}

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

    def stop(state):
        return {}

    builder.add_node("chat", chat)
    builder.add_node("gate", gate)
    builder.add_node("stop", stop)
    builder.add_edge(START, "chat")
    # chat asks for review every time so both WS paths get exercised
    builder.add_edge("chat", "gate")
    builder.add_edge("gate", "stop")
    builder.add_edge("stop", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.fixture()
def client(monkeypatch):
    # The token store is Postgres in production; these fakes keep the suite
    # DB-free while exercising the real check contract (sha256 +
    # compare_digest, 403 on mismatch).
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
    assert body["thread_token"], "thread creation must return a thread token"
    return body["thread_id"], body["thread_token"]


def read_until(socket, wanted_types: set[str], max_frames: int = 60):
    seen = []
    for _ in range(max_frames):
        frame = socket.receive_json()
        seen.append(frame)
        if frame.get("type") in wanted_types:
            return frame, seen
    raise AssertionError(f"never saw {wanted_types}; got {[f.get('type') for f in seen]}")


class TestThreadTokens:
    def test_thread_creation_returns_token(self, client):
        thread_id, token = make_thread(client)
        assert thread_id and token

    def test_state_requires_thread_token(self, client):
        thread_id, token = make_thread(client)
        denied = client.get(f"/api/threads/{thread_id}/state", headers=AUTH_HEADERS)
        assert denied.status_code == 403
        ok = client.get(f"/api/threads/{thread_id}/state", headers=thread_headers(token))
        assert ok.status_code == 200

    def test_wrong_thread_token_rejected(self, client):
        thread_id, _token = make_thread(client)
        denied = client.get(
            f"/api/threads/{thread_id}/state", headers=thread_headers("wrong-token")
        )
        assert denied.status_code == 403

    def test_approvals_require_thread_token(self, client):
        thread_id, _token = make_thread(client)
        denied = client.post(
            f"/api/threads/{thread_id}/approvals",
            json={"approved": True},
            headers=AUTH_HEADERS,
        )
        assert denied.status_code == 403

    def test_approval_with_nothing_waiting_returns_409(self, client):
        # Backend half of the decide() 409 branch: when the gate is gone
        # (interrupt already consumed), the console clears the card
        # instead of restoring a card that can never decide. The
        # frontend branch itself has no JS test harness in this repo;
        # this pins the contract it branches on.
        thread_id, token = make_thread(client)
        response = client.post(
            f"/api/threads/{thread_id}/approvals",
            json={"approved": True},
            headers=thread_headers(token),
        )
        assert response.status_code == 409
        assert "nothing is waiting" in response.json()["detail"]

    def test_history_requires_thread_token(self, client):
        thread_id, _token = make_thread(client)
        denied = client.get(f"/api/threads/{thread_id}/history", headers=AUTH_HEADERS)
        assert denied.status_code == 403

    def test_ws_rejects_bad_thread_token(self, client):
        thread_id, _token = make_thread(client)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(
                f"/ws/{thread_id}", subprotocols=ws_subprotocols("wrong-token")
            ),
        ):
            pass
        assert exc_info.value.code == 4403

    def test_ws_rejects_missing_thread_token(self, client):
        thread_id, _token = make_thread(client)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(
                f"/ws/{thread_id}", subprotocols=["copilot", f"x-api-key.{API_KEY}"]
            ),
        ):
            pass
        assert exc_info.value.code == 4403


class TestRateLimit:
    def test_thread_create_rate_limited(self, client, monkeypatch):
        monkeypatch.setattr(api_main, "THREAD_CREATE_PER_MINUTE", 2)
        assert client.post("/api/threads", headers=AUTH_HEADERS).status_code == 200
        assert client.post("/api/threads", headers=AUTH_HEADERS).status_code == 200
        limited = client.post("/api/threads", headers=AUTH_HEADERS)
        assert limited.status_code == 429


class TestWebsocketTurns:
    def test_streaming_turn_reaches_interrupt(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"

            ws.send_json({"type": "user_message", "content": "post an update on issue 12"})
            _final, frames = read_until(ws, {"done"})
            kinds = [f["type"] for f in frames]
            assert "token" in kinds
            assert any(f.get("nodes") == ["chat"] for f in frames if f["type"] == "node")
            # the stub always routes to the gate afterwards
            interrupt_frames = [f for f in frames if f["type"] == "interrupt"]
            assert interrupt_frames, f"expected interrupt, got {kinds}"
            payload = (
                interrupt_frames[0]["payload"]
                if "payload" in interrupt_frames[0]
                else interrupt_frames[0]["interrupts"][-1]["payload"]
            )
            assert payload["kind"] == "github_comment"

    def test_state_endpoint_reports_pending(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message", "content": "post an update"})
            read_until(ws, {"done"})

        state = client.get(f"/api/threads/{thread}/state", headers=thread_headers(token)).json()
        assert state["awaiting"]["kind"] == "github_comment"
        assert state["message_count"] > 0

    def test_approval_resume_completes_run(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message", "content": "draft the reply"})
            read_until(ws, {"interrupt", "done"})

            response = client.post(
                f"/api/threads/{thread}/approvals",
                json={"approved": True, "note": "ship it", "decided_by": "reviewer-1"},
                headers=thread_headers(token),
            )
            assert response.status_code == 200
            assert response.json()["accepted"] is True

            frame, _frames = read_until(ws, {"done"}, max_frames=80)
            assert frame["type"] == "done"

        history = client.get(f"/api/threads/{thread}/history", headers=thread_headers(token)).json()
        assert len(history) >= 3  # chat -> gate -> stop checkpoints recorded

    def test_unknown_frame_type_errors(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            ws.receive_json()
            ws.send_json({"type": "bogus"})
            frame = ws.receive_json()
            assert frame["type"] == "error"

    def test_non_dict_frame_gets_error_event_not_disconnect(self, client):
        # S19: a JSON array frame must produce an error event; the session
        # must survive it.
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            ws.receive_json()
            ws.send_text("[1, 2, 3]")
            frame = ws.receive_json()
            assert frame["type"] == "error"
            assert "detail" in frame
            ws.send_text("this is not json")
            frame = ws.receive_json()
            assert frame["type"] == "error"
            # session still alive afterwards
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_sequential_turns_both_complete(self, client):
        thread, token = make_thread(client)
        with client.websocket_connect(f"/ws/{thread}", subprotocols=ws_subprotocols(token)) as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message", "content": "first"})
            read_until(ws, {"interrupt", "done"})
            ws.send_json({"type": "user_message", "content": "second"})
            frame, _ = read_until(ws, {"interrupt", "done"})
            assert frame["type"] in {"interrupt", "done"}
