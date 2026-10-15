"""Integration: a stdio MCP server started from the real config serves tools.

Spawns the real ops-db server as a subprocess using the exact config
_server_configs() builds, then calls run_readonly_query against the test
database. On the old code the subprocess got no env, so the call failed
with "OPS_DATABASE_URL is not set". A malformed NO_PROXY is also present
in the parent env to prove it does not leak into the subprocess and break
httpx there. Needs Postgres; skipped when it is not reachable.
"""

import os
import socket

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from ops_copilot.mcp import client as mcp_client
from ops_copilot.settings import get_settings

psycopg = pytest.importorskip("psycopg")


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


async def test_stdio_server_answers_query(monkeypatch):
    if not os.environ.get("OPS_TEST_DATABASE_URL"):
        pytest.skip("OPS_TEST_DATABASE_URL not set")
    _provision_test_db()
    monkeypatch.setenv("OPS_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("OPS_DATABASE_URL", _dsn())
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    get_settings.cache_clear()
    try:
        configs = mcp_client._server_configs()
        client = MultiServerMCPClient(configs)
        tools = await client.get_tools(server_name="ops_db")
        query_tool = next(t for t in tools if t.name == "run_readonly_query")
        result = await query_tool.ainvoke({"sql": "select 1 as one", "max_rows": 10})
    finally:
        get_settings.cache_clear()
    assert "OPS_DATABASE_URL is not set" not in str(result)
    assert "1" in str(result)
