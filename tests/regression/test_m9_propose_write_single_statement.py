"""Regression tests for M9: propose_write must reject stacked SQL at the policy level.

On the original code propose_write never enforces single-statement SQL; a
stacked payload like "UPDATE ...; DELETE ..." passes every check and only
psycopg3's execute() would refuse it at the driver layer. The fixed behavior
runs split_statements at the policy level and requires exactly one statement,
before any database call.

The database is stubbed out: the fake connection records every statement so
the tests prove rejection happens at the policy layer, not in the driver.
"""

import datetime

import pytest

from ops_copilot.mcp.servers import ops_db

STACKED = "UPDATE scratch_notes SET note='x' WHERE id=1; DELETE FROM scratch_notes WHERE id=2"


class _FakeCursor:
    def fetchone(self):
        return (42, datetime.datetime(2026, 1, 1))


class _FakeConn:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return _FakeCursor()


def _stub_connect(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(ops_db, "connect", lambda: fake)
    return fake


def test_propose_write_rejects_stacked_statements(monkeypatch):
    fake = _stub_connect(monkeypatch)
    with pytest.raises(ValueError, match="exactly one"):
        ops_db.propose_write.fn(
            action="update",
            table="scratch_notes",
            sql=STACKED,
            rationale="test rationale",
        )
    assert not any("insert into proposals" in s.lower() for s in fake.statements), (
        "stacked SQL must be rejected before any database write is attempted"
    )


def test_propose_write_rejects_stacked_insert_select(monkeypatch):
    fake = _stub_connect(monkeypatch)
    with pytest.raises(ValueError, match="exactly one"):
        ops_db.propose_write.fn(
            action="insert",
            table="scratch_notes",
            sql="INSERT INTO scratch_notes(note) VALUES ('a'); SELECT * FROM scratch_notes",
            rationale="test rationale",
        )
    assert not any("insert into proposals" in s.lower() for s in fake.statements)


def test_propose_write_accepts_single_statement(monkeypatch):
    # Guard: the fix must not block legitimate single-statement proposals.
    _stub_connect(monkeypatch)
    result = ops_db.propose_write.fn(
        action="update",
        table="scratch_notes",
        sql="UPDATE scratch_notes SET note='x' WHERE id=1",
        rationale="test rationale",
    )
    assert result["status"] == "pending"
    assert result["proposal_id"] == 42
