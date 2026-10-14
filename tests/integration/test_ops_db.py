"""ops-db gateway integration tests.

Requires the compose postgres on localhost:5432; skipped automatically when
it is not reachable so unit-only environments still go green. Runs against
the ops_test database (never the dev/demo one) and provisions its own schema.
"""

import os
import socket

import pytest

psycopg = pytest.importorskip("psycopg")


def _require_dsn(env_name: str) -> str:
    """Test database DSNs come from the environment; fail loudly if unset.

    No hardcoded credential fallback. RuntimeError (not SystemExit) so a
    missing variable surfaces as a normal collection error, not an
    INTERNALERROR.
    """
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
        conn.execute("create table if not exists scratch_notes (id serial primary key, body text)")


_provision_test_db()


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("OPS_DATABASE_URL", _dsn())
    from ops_copilot.mcp.servers import ops_db

    ops_db._conn = None
    ops_db._readonly_conn = None

    # isolate each test with a scratch table and clean proposals
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute("drop table if exists scratch_notes")
        conn.execute("create table scratch_notes (id serial primary key, body text)")
        conn.execute("insert into scratch_notes (body) values ('alpha'), ('beta')")
        conn.execute("delete from applications")
        conn.execute("delete from approvals")
        conn.execute("delete from proposals")
    yield ops_db
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute("drop table if exists scratch_notes")
        conn.execute("delete from applications")
        conn.execute("delete from approvals")
        conn.execute("delete from proposals")


def call(tool, **kwargs):
    return tool.fn(**kwargs)


class TestReadOnlyPath:
    def test_query_returns_structured_rows(self, server):
        result = call(
            server.run_readonly_query, sql="select body from scratch_notes order by id", max_rows=10
        )
        assert result["row_count"] == 2
        assert result["rows"] == [{"body": "alpha"}, {"body": "beta"}]

    @pytest.mark.parametrize(
        "sql",
        [
            "delete from scratch_notes",
            "insert into scratch_notes (body) values ('x')",
            "select 1; select 2",
            "truncate scratch_notes",
            "with t as (select 1) update scratch_notes set body='z'",
            "explain analyze delete from scratch_notes",
        ],
    )
    def test_policy_blocks_writes(self, server, sql):
        with pytest.raises(ValueError):
            call(server.run_readonly_query, sql=sql)

    def test_row_cap(self, server):
        result = call(server.run_readonly_query, sql="select id from scratch_notes", max_rows=1)
        assert result["truncated"] is True
        assert len(result["rows"]) == 1


class TestApprovalChain:
    def _propose(self, server, sql="update scratch_notes set body='gamma' where id=1"):
        return call(
            server.propose_write,
            action="update",
            table="scratch_notes",
            sql=sql,
            rationale="test",
        )["proposal_id"]

    def test_propose_and_apply_flow(self, server):
        pid = self._propose(server)
        with pytest.raises(ValueError, match="pending"):
            call(server.apply_approved_write, proposal_id=pid)

        approve_in_db(pid)
        outcome = call(server.apply_approved_write, proposal_id=pid)
        assert outcome["rows_affected"] == 1

        rows = query("select body from scratch_notes where id = 1")
        assert rows[0]["body"] == "gamma"

    def test_double_apply_refused(self, server):
        pid = self._propose(server)
        approve_in_db(pid)
        call(server.apply_approved_write, proposal_id=pid)
        # Either the status gate or the applications-table check stops it.
        with pytest.raises(ValueError, match=r"already applied|status 'applied'"):
            call(server.apply_approved_write, proposal_id=pid)

    def test_proposal_action_must_match_sql(self, server):
        with pytest.raises(ValueError, match="does not match"):
            call(
                server.propose_write,
                action="delete",
                table="scratch_notes",
                sql="update scratch_notes set body='x'",
                rationale="mismatched",
            )

    def test_unknown_table_rejected(self, server):
        with pytest.raises(ValueError):
            call(server.describe_table, table="no_such_table_anywhere")

    def test_identifier_injection_rejected(self, server):
        with pytest.raises(ValueError):
            call(
                server.profile_column, table="scratch_notes; drop table scratch_notes", column="id"
            )


def approve_in_db(proposal_id: int) -> None:
    """What the API's approval endpoint does before resuming the graph."""
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(
            "insert into approvals (proposal_id, approved, note, decided_by) values (%s, true, 'test', 'pytest')",
            (proposal_id,),
        )
        conn.execute("update proposals set status = 'approved' where id = %s", (proposal_id,))


def query(sql: str):
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        cur = conn.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
