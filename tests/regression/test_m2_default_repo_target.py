"""Regression tests for M2: the minted token target must match the server target.

On the original code the gate mints the token target from the raw payload
repo (f"{payload.get('repo')}#{payload.get('number')}"), so an omitted repo
produces "None#5" or "#5", while post_comment verifies against the
_repo_path()-normalized repo ("psf/requests#5"). Approved default-repo
comments are always rejected. The fixed behavior normalizes the repo in the
gate before minting, so the minted target verifies against the server target.

These drive the real approval_gate with a stubbed interrupt (no graph, no
network, no database) and check the minted token with the real mint/verify
pair plus the real server-side _repo_path normalization.
"""

import asyncio

from ops_copilot import security
from ops_copilot.graph import gate
from ops_copilot.mcp.servers import source_control
from ops_copilot.settings import get_settings

BODY = "Looks good, shipping it."
NUMBER = 5


def _approved_state(payload):
    return {"awaiting": payload, "tasks": [], "messages": [], "error": None}


def _mint_via_gate(monkeypatch, payload):
    """Run the real approval_gate with a stubbed interrupt; return the token."""
    monkeypatch.setattr(
        gate, "interrupt", lambda payload: {"approved": True, "note": "", "decided_by": "tester"}
    )
    cmd = asyncio.run(gate.approval_gate(_approved_state(payload)))
    assert cmd.goto == "executor"
    token = cmd.update["awaiting"].get("approval_token")
    assert token, "gate must mint an approval token for github_comment payloads"
    return token


def _server_target(repo, number):
    return f"{source_control._repo_path(repo)}#{number}"


def test_omitted_repo_token_verifies_against_server_target(monkeypatch):
    payload = {"kind": "github_comment", "number": NUMBER, "body": BODY}
    token = _mint_via_gate(monkeypatch, payload)
    ok, reason = security.verify(
        secret=get_settings().approval_secret,
        token=token,
        action="github_comment",
        target=_server_target(None, NUMBER),
        content=BODY,
    )
    assert ok, f"token minted for omitted repo must verify against the server target: {reason}"


def test_empty_repo_token_verifies_against_server_target(monkeypatch):
    payload = {"kind": "github_comment", "repo": "", "number": NUMBER, "body": BODY}
    token = _mint_via_gate(monkeypatch, payload)
    ok, reason = security.verify(
        secret=get_settings().approval_secret,
        token=token,
        action="github_comment",
        target=_server_target("", NUMBER),
        content=BODY,
    )
    assert ok, f"token minted for empty repo must verify against the server target: {reason}"


def test_explicit_repo_still_round_trips(monkeypatch):
    # Guard: the normal explicit-repo path keeps working after the fix.
    payload = {"kind": "github_comment", "repo": "psf/requests", "number": NUMBER, "body": BODY}
    token = _mint_via_gate(monkeypatch, payload)
    ok, reason = security.verify(
        secret=get_settings().approval_secret,
        token=token,
        action="github_comment",
        target=_server_target("psf/requests", NUMBER),
        content=BODY,
    )
    assert ok, f"explicit-repo token must keep verifying: {reason}"


def test_repo_path_defaults_match_gate_and_server():
    # The contract both sides must honor: omitted repo means the default repo.
    assert source_control._repo_path(None) == source_control._repo_path("")
    assert _server_target(None, NUMBER) == "psf/requests#5"
