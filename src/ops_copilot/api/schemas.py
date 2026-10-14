from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ThreadCreated(BaseModel):
    thread_id: str
    # Per-thread ownership token (S4). The console and any client must send
    # it back as X-Thread-Token on thread-scoped endpoints and as the
    # x-thread-token.<tok> WebSocket subprotocol. Only the sha256 of the
    # token is stored server-side.
    thread_token: str


class ProposalView(BaseModel):
    """The exact SQL of a registered proposal, for the human reviewer.

    Served by GET /threads/{id}/proposals/{proposal_id}; the SQL lives here
    and in the proposals table only, never in checkpoints, audit records, or
    /state payloads (S6).
    """

    proposal_id: int
    action: str
    table: str
    sql: str
    status: str


class TaskView(BaseModel):
    id: str
    description: str
    assignee: str
    status: str
    summary: str = ""


class StateView(BaseModel):
    thread_id: str
    tasks: list[TaskView] = []
    awaiting: dict[str, Any] | None = None
    error: str | None = None
    message_count: int = 0


class CheckpointView(BaseModel):
    checkpoint_id: str
    step: int
    created_at: datetime | None = None
    next_nodes: list[str] = []


class ReplayRequest(BaseModel):
    checkpoint_id: str


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""
    # S12: caller-asserted; only as trustworthy as the shared API key.
    # This deployment has no per-user identity.
    decided_by: str = "reviewer"


class ApprovalAck(BaseModel):
    thread_id: str
    accepted: bool
    detail: str = ""


class AuditRecord(BaseModel):
    ts: str
    kind: str
    actor: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: str
    app_db: bool
    checkpoint_db: bool
    version: str
