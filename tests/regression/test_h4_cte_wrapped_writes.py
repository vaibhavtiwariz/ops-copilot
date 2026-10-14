"""Regression tests for H4: enforce_read_only must reject CTE-wrapped writes.

On the original code the WITH-branch of the policy only inspects the first
closing paren, so a write tucked into a later CTE is classified "select" and
allowed. The fixed behavior rejects modifying verbs anywhere inside the WITH
clause. guardrails.py is dependency-free, so these run with no services.
"""

import pytest

from ops_copilot.guardrails import SqlPolicyError, enforce_read_only


def test_cte_wrapped_delete_from_brief_is_rejected():
    # Exact payload from the findings brief.
    payload = "with a as (select 1), b as (delete from scratch_notes returning *) select * from b"
    with pytest.raises(SqlPolicyError):
        enforce_read_only(payload)


def test_cte_wrapped_update_is_rejected():
    payload = "with a as (update scratch_notes set note='x' returning *) select * from a"
    with pytest.raises(SqlPolicyError):
        enforce_read_only(payload)


def test_cte_wrapped_insert_is_rejected():
    payload = "with a as (insert into scratch_notes(note) values ('x') returning *) select * from a"
    with pytest.raises(SqlPolicyError):
        enforce_read_only(payload)


def test_plain_read_cte_still_allowed():
    # Guard: the fix must keep legitimate read-only CTEs working.
    payload = "with a as (select 1 as n), b as (select n + 1 as m from a) select * from b"
    assert enforce_read_only(payload).lower().startswith("with")
