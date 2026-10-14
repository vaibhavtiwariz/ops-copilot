"""Regression tests for H2: replay must not re-execute past the approval gate.

On the original code the replay endpoint accepted any checkpoint_id, and
replaying from a post-gate checkpoint re-ran the executor: the approval
token for a GitHub comment sits in checkpoint state and stays valid for its
TTL, so a replay posted a duplicate public comment. The fixed behavior
refuses replay from a checkpoint whose next nodes include the executor
(HTTP 409), while replays from earlier checkpoints still proceed.

The app is built without running lifespan; the runner's graph is stubbed so
no database or model is involved.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ops_copilot.api import main as api_main
from ops_copilot.api.ratelimit import RateLimiter
from ops_copilot.api.schemas import ReplayRequest

API_KEY = "test-key-not-real"


def _replay_endpoint():
    app = api_main.create_app(settings=SimpleNamespace(api_key=API_KEY))
    candidates = list(app.routes)
    for route in app.routes:
        # Newer FastAPI wraps included routers; the real routes live inside.
        original = getattr(route, "original_router", None)
        if original is not None:
            candidates.extend(original.routes)
    route = next(
        r for r in candidates if getattr(r, "path", "") == "/api/threads/{thread_id}/replay"
    )
    return route.endpoint


class _StubGraph:
    def __init__(self, next_nodes):
        self._next = next_nodes

    async def aget_state(self, config):
        return SimpleNamespace(next=self._next)


class _StubRunner:
    def __init__(self, next_nodes):
        self.graph = _StubGraph(next_nodes)
        self.replays = []

    async def run_turn(self, thread_id, content="", replay_from=None):
        self.replays.append(replay_from)
        yield {"type": "done"}


def _stub_request(runner, monkeypatch):
    settings = SimpleNamespace(api_key=API_KEY)
    state = SimpleNamespace(settings=settings, runner=runner, limiter=RateLimiter())
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    token_check_calls = []

    # S4 added a thread-token gate to this endpoint after this test was
    # written. Stub it out so the test exercises the H2 replay guard,
    # not the token gate (token behavior has its own tests).
    async def _no_token_check(request, thread_id, provided):
        token_check_calls.append((thread_id, provided))
        return None

    monkeypatch.setattr(api_main, "_check_thread_token", _no_token_check)
    request.token_check_calls = token_check_calls
    return request


def _replay(runner, monkeypatch):
    endpoint = _replay_endpoint()
    request = _stub_request(runner, monkeypatch)
    result = asyncio.run(
        endpoint(
            thread_id="thread-1",
            body=ReplayRequest(checkpoint_id="ckpt-1"),
            request=request,
            x_api_key=API_KEY,
        )
    )
    return result, request


def test_replay_from_post_gate_checkpoint_is_refused(monkeypatch):
    runner = _StubRunner(next_nodes=("executor",))
    with pytest.raises(HTTPException) as exc_info:
        _replay(runner, monkeypatch)
    assert exc_info.value.status_code == 409
    assert runner.replays == [], "refused replay must not re-execute anything"


def test_replay_from_pre_gate_checkpoint_proceeds(monkeypatch):
    # Guard: time travel from before the gate keeps working after the fix.
    runner = _StubRunner(next_nodes=("supervisor",))
    result, request = _replay(runner, monkeypatch)
    assert result["event_count"] == 1
    assert runner.replays == ["ckpt-1"]
    # The S4 thread-token gate must still sit in front of the endpoint.
    assert len(request.token_check_calls) == 1
    assert request.token_check_calls[0][0] == "thread-1"
