"""Regression tests for M1 (and the H2 mechanism): approval tokens are single-use.

On the original code a minted token verified any number of times inside its
TTL, so a token captured from checkpoint state (for example via the replay
endpoint in H2) could post the same GitHub comment repeatedly. The fixed
design binds a unique jti into every minted token, rejects tokens without
one, and keeps a used-token ledger in post_comment so a token can only ever
produce one post.

The GitHub HTTP call is stubbed out; no network, no database.
"""

import json

import pytest

from ops_copilot import security
from ops_copilot.mcp.servers import source_control

SECRET = "test-single-use-secret"
TARGET = "psf/requests#5"
BODY = "hello"


class _FakeResponse:
    status_code = 201
    text = "created"

    def json(self):
        return {"html_url": "https://github.com/psf/requests/issues/5#issuecomment-1"}


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, path, json=None):
        assert path == "/repos/psf/requests/issues/5/comments"
        return _FakeResponse()


def _stub_http(monkeypatch):
    monkeypatch.setenv("OPS_APPROVAL_SECRET", SECRET)
    monkeypatch.setattr(source_control, "_client", lambda: _FakeClient())


def _mint():
    return security.mint(secret=SECRET, action="github_comment", target=TARGET, content=BODY)


def test_approval_token_cannot_post_twice(monkeypatch):
    _stub_http(monkeypatch)
    token = _mint()
    first = source_control.post_comment.fn(
        repo="psf/requests", number=5, body=BODY, approval_token=token
    )
    assert first["posted"] is True
    with pytest.raises(ValueError, match="already used"):
        source_control.post_comment.fn(
            repo="psf/requests", number=5, body=BODY, approval_token=token
        )


def test_second_distinct_token_still_posts(monkeypatch):
    # Guard: single-use must be per token, not a global latch.
    _stub_http(monkeypatch)
    first = source_control.post_comment.fn(
        repo="psf/requests", number=5, body=BODY, approval_token=_mint()
    )
    second = source_control.post_comment.fn(
        repo="psf/requests", number=5, body=BODY, approval_token=_mint()
    )
    assert first["posted"] is True
    assert second["posted"] is True


def test_token_without_jti_is_rejected():
    # Pre-fix tokens carried no jti and can never be single-use; the fixed
    # verifier treats them as invalid outright.
    raw_token = _mint()
    raw, _ = raw_token.rsplit(".", 1)
    claims = json.loads(raw)
    claims.pop("jti", None)  # ensure the forged token carries no jti
    forged_raw = json.dumps(claims, separators=(",", ":"), sort_keys=True)
    forged = f"{forged_raw}.{security._sign(forged_raw.encode(), SECRET)}"
    ok, reason = security.verify(
        secret=SECRET,
        token=forged,
        action="github_comment",
        target=TARGET,
        content=BODY,
    )
    assert not ok, f"pre-fix token without jti verified: {reason}"


def test_fresh_token_still_verifies():
    # Guard: the jti requirement must not break normal first verification.
    ok, reason = security.verify(
        secret=SECRET,
        token=_mint(),
        action="github_comment",
        target=TARGET,
        content=BODY,
    )
    assert ok, f"fresh token must verify: {reason}"
