# Architecture

```
                                   ┌──────────────────────────────────────────┐
                                   │                Next.js 14                │
                                   │   chat stream · task plan · approval UI  │
                                   └──────────────┬───────────────────────────┘
                                                  │ WebSocket + REST
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                  FastAPI                                        │
│  /ws/{thread}      token + node + interrupt streaming (per-thread fan-out bus)  │
│  /api/threads/*/approvals    human decisions -> resume the paused graph         │
│  /api/threads/*/history      checkpoint timeline (time-travel)                  │
│  /api/audit                  append-only trail of tool calls and decisions       │
│                                                                                  │
│  ┌───────────────────────────── LangGraph (Postgres checkpointer) ────────────┐ │
│  │                                                                            │ │
│  │        ┌──────────── supervisor: plans, dispatches, synthesizes ───────┐   │ │
│  │        │                                                               │   │ │
│  │        ▼              ▼                        ▼                       │   │ │
│  │    ┌────────┐    ┌────────┐               ┌────────┐                   │   │ │
│  │    │analyst │    │ devops │               │ triage │  workers = RBAC-  │   │ │
│  │    └───┬────┘    └───┬────┘               └───┬────┘  scoped tool sets  │   │ │
│  │        └──────────────┴───────────────────────┘                        │   │ │
│  │                       │ sensitive proposal?                            │   │ │
│  │                       ▼                                                │   │ │
│  │             ┌──────────────────┐     approved    ┌───────────┐         │   │ │
│  │             │  approval_gate   ├──────────────►  executor  │         │   │ │
│  │             │ interrupt() pause│                 └───────────┘         │   │ │
│  │             └──────────────────┘                                       │   │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                    │                                    │                       │
│                    │ MCP (streamable-http / stdio)      │                       │
│                    ▼                                    ▼                       │
│        ┌────────────────────────┐            ┌────────────────────────┐        │
│        │  ops-db gateway        │            │ source-control server  │        │
│        │ read-only SQL fence    │            │ GitHub REST, cached    │        │
│        │ proposals + apply gate │            │ drafts only; posts are │        │
│        │ row/time caps, PII mask│            │ HMAC-token gated       │        │
│        └───────────┬────────────┘            └───────────┬────────────┘        │
└────────────────────┼────────────────────────────────────┼──────────────────────┘
                     ▼                                    ▼
              Postgres (ops data,                  api.github.com
              proposals/approvals,
              checkpoints)
```

## Data flow of a mutation request

1. User asks for something destructive ("refund order 42").
2. Supervisor delegates to a worker; the worker's tools cannot execute writes.
3. Worker calls `propose_write`; the gateway stores exact SQL in `proposals`.
4. Worker returns to the supervisor with `awaiting` set; the graph funnels into
   `approval_gate`, which calls `interrupt()` - execution pauses here.
5. The API surfaces the proposal (SQL, table, rationale) over the websocket.
6. A human POSTs a decision. On approve, the API flips the proposals row and
   resumes the graph with `Command(resume=...)` - same thread, same state.
7. `executor` invokes `apply_approved_write(proposal_id)`; the gateway checks
   status + idempotency inside the applying transaction.
8. Every step lands in the audit log.

Rejecting at step 6 marks the task rejected; nothing was ever executable.

## Module map

| path | responsibility |
|---|---|
| `src/ops_copilot/graph/` | state schema, supervisor, workers, gate, builder |
| `src/ops_copilot/mcp/servers/` | FastMCP servers (ops-db, source-control) |
| `src/ops_copilot/mcp/client.py` | role-based tool loading for workers |
| `src/ops_copilot/guardrails.py` | SQL policy, PII masking, token budget |
| `src/ops_copilot/security.py` | HMAC approval tokens |
| `src/ops_copilot/api/` | FastAPI app, event bus, graph runner |
| `evals/` | golden-trajectory harness + optional live judge |
