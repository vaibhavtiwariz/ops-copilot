"""Append-only audit trail for tool calls and human decisions.

Every side-effecting action (proposals, approvals, applications) lands here
before anything else happens. The JSONL sink keeps the MCP servers dependency
free; the API additionally writes structured rows into Postgres so approvals
can be referenced by id from inside the database gateway.

Each record carries "prev_hash" (sha256 of the previous record's canonical
JSON, "GENESIS" for the first) and "hash" (sha256 of the record including
prev_hash), so a tampered line breaks the chain for every record after it.

Trust boundary, stated plainly: the chain makes tampering evident to an
append-only attacker who can add or alter lines but cannot rewrite the whole
file consistently. It is NOT tamper-proof against someone with full write
access to the file, who can recompute a valid chain from scratch. The
structured Postgres rows remain the harder source of truth for approvals.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

_GENESIS = "GENESIS"

# Hash of the most recent emitted record per file, resumed from each file's
# last line on first emit so chains survive process restarts and appends.
# Keyed by absolute path: two AuditLog instances on different files must not
# share chain state (the test suite runs several in one process).
_last_hash_by_path: dict[str, str] = {}


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def _resume_chain(path: Path) -> str:
    """Hash of the last chained record in an existing file, or GENESIS."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _GENESIS
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        chained = record.get("hash")
        return str(chained) if chained else _GENESIS
    return _GENESIS


AuditEventKind = Literal[
    "tool_call",
    "proposal",
    "approval",
    "rejection",
    "apply",
    "budget_stop",
]


class AuditLog:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self, kind: AuditEventKind, *, actor: str, summary: str, detail: dict[str, Any]
    ) -> None:
        key = os.path.abspath(self.path)
        if key not in _last_hash_by_path:
            _last_hash_by_path[key] = _resume_chain(self.path)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": kind,
            "actor": actor,
            "summary": summary,
            "detail": _jsonable(detail),
            "prev_hash": _last_hash_by_path[key],
        }
        record["hash"] = hashlib.sha256(_canonical(record).encode()).hexdigest()
        _last_hash_by_path[key] = record["hash"]
        line = json.dumps(record, separators=(",", ":"))
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            # The structured Postgres rows remain the source of truth; losing
            # the JSONL mirror must never take down an approval request.
            log.error("audit_write_failed", path=str(self.path), error=str(exc))
        log.info("audit", **record)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return repr(value)


def load_audit(path: str | os.PathLike[str], limit: int = 200) -> list[dict[str, Any]]:
    """Read the most recent records; used by the /audit endpoint and demos."""
    audit_path = Path(path)
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
