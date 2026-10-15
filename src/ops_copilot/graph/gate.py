"""Human-in-the-loop gate and the executor for approved actions.

gate: turns a pending proposal into an interrupt(). The graph pauses, the API
surfaces the payload to whoever approves, and resuming with an
ApprovalDecision lands right here. Rejected work never leaves the building.

executor: the only place from which approved side effects actually run.
"""

import json
from typing import Any, Literal

from langchain_core.messages import AIMessage
from langgraph.types import Command, interrupt

from ops_copilot.audit import AuditLog
from ops_copilot.logging import get_logger
from ops_copilot.mcp.client import tool_by_name
from ops_copilot.repo import normalize_repo
from ops_copilot.security import mint
from ops_copilot.settings import get_settings

from .state import AgentState, Task

log = get_logger(__name__)

_approval_log: AuditLog | None = None


def _audit() -> AuditLog:
    global _approval_log
    if _approval_log is None:
        settings = get_settings()
        _approval_log = AuditLog(settings.audit_log_path)
    return _approval_log


def _task_for(state: AgentState) -> Task | None:
    tasks = state.get("tasks") or []
    return next((t for t in tasks if t.status == "in_progress"), None)


async def approval_gate(state: AgentState) -> Command[Literal["supervisor", "executor"]]:
    payload = state.get("awaiting") or {}
    # Graph pauses here; whatever the human submits comes back as this value.
    decision = interrupt(payload)
    approved = bool(decision.get("approved"))
    note = str(decision.get("note", ""))[:500]
    decided_by = str(decision.get("decided_by") or "human")[:120]

    task = _task_for(state)

    if not approved:
        # The decision record was already emitted by the API endpoint; the
        # gate must not emit a second one.
        updates: dict[str, Any] = {
            "awaiting": None,
            "messages": [
                AIMessage(
                    content=f"[review] The proposed action was declined by {decided_by}."
                    + (f" Note: {note}" if note else "")
                )
            ],
        }
        if task is not None:
            updates["tasks"] = [
                task.model_copy(
                    update={
                        "status": "rejected",
                        "summary": f"declined by reviewer: {note or 'no note'}",
                    }
                )
            ]
        return Command(update=updates, goto="supervisor")

    prepared = dict(payload)
    prepared["decision_note"] = note
    prepared["decided_by"] = decided_by

    if payload.get("kind") == "github_comment":
        secret = get_settings().approval_secret
        if not secret:
            return _reject_internally(
                task, "approval secret is not configured on the API; cannot sign comment"
            )
        # Resolve the default repo before minting so the token target matches
        # what the server verifies, and mint over the stripped body so the
        # content hash matches what the server checks on post.
        repo = normalize_repo(payload.get("repo"))
        body = str(payload.get("body") or "").strip()
        prepared["repo"] = repo
        # Store the exact text the token was minted over, so what the human
        # reviews is byte-identical to what gets verified and posted.
        prepared["body"] = body
        target = f"{repo}#{payload.get('number')}"
        prepared["approval_token"] = mint(
            secret=secret, action="github_comment", target=target, content=body
        )

    # The decision record was already emitted by the API endpoint; the gate
    # must not emit a second one. The executor's "apply" record still follows.
    return Command(update={"awaiting": prepared}, goto="executor")


async def executor(state: AgentState) -> Command[Literal["supervisor"]]:
    payload = state.get("awaiting") or {}
    kind = payload.get("kind")
    task = _task_for(state)

    def parse_outcome(raw: Any) -> dict[str, Any]:
        """MCP results arrive as content-block lists or JSON strings."""
        if isinstance(raw, str):
            parsed = json.loads(raw)
        elif isinstance(raw, list):
            texts = [
                b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text"
            ]
            parsed = json.loads("\n".join(t for t in texts if t)) if texts else raw
        else:
            parsed = raw
        if isinstance(parsed, list):
            parsed = next((item for item in parsed if isinstance(item, dict)), {})
        return parsed if isinstance(parsed, dict) else {}

    try:
        if kind == "db_write":
            tool = await tool_by_name("ops_db", "apply_approved_write")
            if tool is None:
                raise RuntimeError("ops_db server did not expose apply_approved_write")
            outcome = parse_outcome(
                await tool.ainvoke({"proposal_id": int(payload["proposal_id"])})
            )
            summary = (
                f"applied proposal #{payload['proposal_id']}: "
                f"{outcome.get('rows_affected', '?')} row(s) on {outcome.get('table')}"
            )
        elif kind == "github_comment":
            tool = await tool_by_name("source_control", "post_comment")
            if tool is None:
                raise RuntimeError("source_control server did not expose post_comment")
            outcome = parse_outcome(
                await tool.ainvoke(
                    {
                        "repo": payload["repo"],
                        "number": int(payload["number"]),
                        "body": payload["body"],
                        "approval_token": payload["approval_token"],
                    }
                )
            )
            summary = (
                f"posted approved comment on {payload['repo']}#{payload['number']} "
                f"(posted={outcome.get('posted')})"
            )
        else:
            raise RuntimeError(f"unknown gated action kind {kind!r}")
    except Exception as exc:
        log.error("executor_failed", kind=kind, error=str(exc))
        _audit().emit(
            "apply", actor="system", summary=f"execution failed: {exc}", detail={"payload": payload}
        )
        updates: dict[str, Any] = {
            "awaiting": None,
            "error": f"approved action failed to execute: {exc}",
            "messages": [AIMessage(content=f"[execution] Approved action failed: {exc}")],
        }
        if task is not None:
            updates["tasks"] = [
                task.model_copy(update={"status": "failed", "summary": f"execution failed: {exc}"})
            ]
        return Command(update=updates, goto="supervisor")

    _audit().emit(
        "apply",
        actor=str(payload.get("decided_by") or "system"),
        summary=summary,
        detail={"kind": kind, "outcome": outcome},
    )
    updates = {
        "awaiting": None,
        "messages": [AIMessage(content=f"[executed] {summary}")],
    }
    if task is not None:
        updates["tasks"] = [task.model_copy(update={"status": "done", "summary": summary})]
    return Command(update=updates, goto="supervisor")


def _reject_internally(
    task: Task | None, reason: str
) -> Command[Literal["supervisor", "executor"]]:
    updates: dict[str, Any] = {
        "awaiting": None,
        "messages": [AIMessage(content=f"[review] Could not proceed: {reason}")],
    }
    if task is not None:
        updates["tasks"] = [task.model_copy(update={"status": "rejected", "summary": reason})]
    return Command(update=updates, goto="supervisor")
