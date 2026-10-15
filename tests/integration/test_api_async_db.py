"""Regression tests: the API's async DB reads must await fetchone().

The API talks to Postgres through psycopg3's async pool, where fetchone()
is itself a coroutine. Three call sites used it without await, so each one
got a coroutine object instead of a row: the proposal endpoint 503d, the
thread-token lookup always failed (WebSocket always 403, HTTP endpoints
503), and the approval insert was silently skipped (the coroutine is never
None, so the code thought an approvals row already existed).

These tests exercise the real helpers against a real async pool with no
monkeypatching of the helpers themselves, so they fail on the unfixed code
and pass on the fixed code. They need Postgres; they are skipped when it
is not reachable.
"""

import hashlib
import os
import socket
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

import ops_copilot.api.main as api_main
from ops_copilot.api.schemas import ApprovalRequest
from ops_copilot.settings import Settings, get_settings

psycopg = pytest.importorskip("psycopg")

API_KEY = "test-key-not-real"


def _require_dsn(env_name: str) -> str:
    dsn = os.environ.get(env_name)
    if not dsn:
        raise RuntimeError(
            f"{env_name} is not set; DB integration tests refuse to use a hardcoded "
            f"credential. Export it first, e.g. "
            f"{env_name}=postgresql://<user>:<pass>@localhost:5432/<db>"
        )
    return dsn


def _dsn() -> str:
    return _require_dsn("OPS_TEST_DATABASE_URL")


def _admin_dsn() -> str:
    return _require_dsn("OPS_TEST_ADMIN_DSN")


def postgres_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not postgres_available(), reason="postgres not running")


def _provision_test_db() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from bootstrap_db import SCHEMA_SQL

    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = 'ops_test'").fetchone()
        if not exists:
            conn.execute("create database ops_test")
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)


@pytest.fixture()
async def pool():
    if not os.environ.get("OPS_TEST_DATABASE_URL"):
        pytest.skip("OPS_TEST_DATABASE_URL not set")
    _provision_test_db()
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    test_pool = AsyncConnectionPool(
        _dsn(), kwargs={"autocommit": True, "row_factory": dict_row}, open=False
    )
    await test_pool.open()
    async with test_pool.connection() as conn:
        await conn.execute("delete from applications")
        await conn.execute("delete from approvals")
        await conn.execute("delete from proposals")
        await conn.execute("delete from thread_tokens")
    yield test_pool
    await test_pool.close()


def _fake_request(test_pool, settings=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                app_pool=test_pool,
                settings=settings or Settings(api_key=API_KEY),
            )
        )
    )


async def _seed_proposal(test_pool, status="pending"):
    async with test_pool.connection() as conn:
        cursor = await conn.execute(
            "insert into proposals (action, table_name, sql_text, status) "
            "values (%s, %s, %s, %s) returning id",
            ("update", "orders", "update orders set status = 'refunded' where id = 1", status),
        )
        row = await cursor.fetchone()
        return row["id"]


async def _seed_thread_token(test_pool, thread_id, token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    async with test_pool.connection() as conn:
        await conn.execute(
            "insert into thread_tokens (thread_id, token_hash) values (%s, %s)",
            (thread_id, token_hash),
        )
    return token


async def test_lookup_token_hash_returns_stored_hash(pool):
    token = "test-token-abc"
    await _seed_thread_token(pool, "thread-1", token)
    expected = hashlib.sha256(token.encode()).hexdigest()
    assert await api_main._lookup_token_hash(pool, "thread-1") == expected
    assert await api_main._lookup_token_hash(pool, "missing-thread") == ""


async def test_record_db_approval_inserts_approval_row(pool):
    proposal_id = await _seed_proposal(pool)
    request = _fake_request(pool)
    body = ApprovalRequest(approved=True, note="ok", decided_by="reviewer-1")
    assert await api_main._record_db_approval(request, proposal_id, body) is True
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "select approved from approvals where proposal_id = %s", (proposal_id,)
        )
        approval = await cursor.fetchone()
        assert approval is not None
        assert approval["approved"] is True
        cursor = await conn.execute("select status from proposals where id = %s", (proposal_id,))
        proposal = await cursor.fetchone()
        assert proposal["status"] == "approved"


async def test_record_db_approval_is_idempotent(pool):
    proposal_id = await _seed_proposal(pool)
    request = _fake_request(pool)
    body = ApprovalRequest(approved=True, note="ok", decided_by="reviewer-1")
    assert await api_main._record_db_approval(request, proposal_id, body) is True
    assert await api_main._record_db_approval(request, proposal_id, body) is True
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "select count(*) as n from approvals where proposal_id = %s", (proposal_id,)
        )
        row = await cursor.fetchone()
        assert row["n"] == 1


async def test_thread_proposal_endpoint_returns_sql(pool, monkeypatch):
    """The proposal endpoint runs the real lookup against the real pool.

    Goes through TestClient (the endpoint is a closure inside create_app,
    not a module function) with no monkeypatching of the lookup helpers.
    On the unfixed code the request 503s; on the fixed code it returns
    the proposal SQL.
    """
    proposal_id = await _seed_proposal(pool)
    token = await _seed_thread_token(pool, "thread-1", "test-token-abc")
    monkeypatch.setenv("OPS_DATABASE_URL", _dsn())
    monkeypatch.setenv("OPS_API_KEY", API_KEY)
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password-not-real")
    get_settings.cache_clear()
    try:
        app = api_main.create_app(graph=_stub_graph())
        with TestClient(app) as client:
            response = client.get(
                f"/api/threads/thread-1/proposals/{proposal_id}",
                headers={"X-API-Key": API_KEY, "X-Thread-Token": token},
            )
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["proposal_id"] == proposal_id
    assert body["action"] == "update"
    assert body["table"] == "orders"
    assert body["sql"] == "update orders set status = 'refunded' where id = 1"
    assert body["status"] == "pending"


class _StubState(TypedDict):
    messages: Annotated[list, lambda a, b: a + b]


def _stub_graph():
    """Minimal graph so create_app skips building the real checkpointer."""
    builder = StateGraph(_StubState)

    async def chat(state):
        return {"messages": [AIMessage(content="stub")]}

    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=InMemorySaver())
