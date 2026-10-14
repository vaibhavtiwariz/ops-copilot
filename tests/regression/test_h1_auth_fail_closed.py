"""Regression tests for H1: the API must fail closed when no key is configured.

On the original code a default deploy with OPS_API_KEY unset left every
guarded endpoint open: _check_key only rejected when a key was configured,
and nothing stopped the app from booting without one. The fixed behavior
refuses to boot at all without OPS_API_KEY (lifespan raises RuntimeError),
so an unauthenticated deployment cannot exist.

The boot check is exercised directly against lifespan with a stubbed
settings object; the database pool is stubbed to explode so the original
code fails fast instead of hanging on a real connection attempt.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ops_copilot.api import main as api_main
from ops_copilot.api.main import _check_key


def _stub_settings(api_key):
    return SimpleNamespace(
        api_key=api_key,
        audit_log_path="/tmp/ops-copilot-test-audit.jsonl",
        checkpoint_db_url="postgresql://ops:ops@localhost:5432/ops_test_checkpoints",
    )


class _ExplodingPool:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("no database in unit tests")


async def _enter_lifespan(app):
    async with api_main.lifespan(app):
        pass


def test_api_refuses_to_boot_without_api_key(monkeypatch):
    monkeypatch.setattr(api_main, "get_settings", lambda: _stub_settings(None))
    monkeypatch.setattr(api_main, "AsyncConnectionPool", _ExplodingPool)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError, match="OPS_API_KEY"):
        asyncio.run(_enter_lifespan(app))


def test_api_boot_check_passes_key_check_with_key_set(monkeypatch):
    # Guard: with a key configured, the boot check itself must not be the
    # thing that stops startup (the DB pool stub still explodes afterwards,
    # which is exactly what we want to see).
    monkeypatch.setattr(api_main, "get_settings", lambda: _stub_settings("s3cret"))
    monkeypatch.setattr(api_main, "AsyncConnectionPool", _ExplodingPool)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError, match="no database in unit tests"):
        asyncio.run(_enter_lifespan(app))


def _stub_request(api_key):
    settings = SimpleNamespace(api_key=api_key)
    state = SimpleNamespace(settings=settings)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_check_key_accepts_correct_key():
    # Guard: the per-request check keeps working for the configured key.
    _check_key(_stub_request("s3cret"), "s3cret")


def test_check_key_rejects_wrong_key():
    with pytest.raises(HTTPException) as exc_info:
        _check_key(_stub_request("s3cret"), "wrong")
    assert exc_info.value.status_code == 401


def test_check_key_rejects_missing_key_when_configured():
    with pytest.raises(HTTPException) as exc_info:
        _check_key(_stub_request("s3cret"), None)
    assert exc_info.value.status_code == 401
