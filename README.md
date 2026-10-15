# ops·copilot

An enterprise operations copilot where a supervisor agent plans work, three
scoped specialists execute it through MCP-exposed tools, and **nothing
destructive happens without a human pressing a button**, with every step
checkpointed, replayable, and audited.

Built to answer the questions that actually come up in staff-level AI
engineering interviews: how do you bound runaway agents, how do you make side
effects safe, how do you test non-determinism, what survives a restart.

```
supervisor ──► analyst   read-only SQL over ops data (fenced gateway)
           ──► devops    GitHub issues / PR diffs / repo health
           ──► triage    incident lookups + customer-response drafts
      all of the above ──► approval gate (interrupt) on sensitive actions
```

## The interesting part: mutations are structurally impossible

Workers have no tool that can write to the database. The only path is:

1. worker registers a proposal (exact SQL lands in a `proposals` row),
2. the graph pauses inside `interrupt()`, durable, survives restarts,
3. a human sees the SQL itself in the UI and approves or rejects,
4. only then does `apply_approved_write` run it, verified against the
   approvals table, idempotent via an `applications` row,
5. everything above is in an append-only audit log.

The read path has its own fence: one statement per call, CTE-wrapped writes
refused by the policy layer (with the driver-level read-only session as a
second layer that refuses them too), write verbs scanned across the whole
statement body including subqueries, `SELECT ... INTO` and `FOR UPDATE`
refused, `EXPLAIN ANALYZE <write>` classified as
a write and refused, side-channel
functions (`pg_sleep`, `dblink`, `pg_read_file`…) denied by name outside
string literals, session-level `read_only` + statement timeout + row caps.
This exists because Anthropic's reference postgres MCP server was archived in
2025 after exactly that kind of read-only escape; [ADR 2](docs/adrs/0002-custom-db-gateway.md)
is the writeup.

## Architecture

Full diagram and data flow: [docs/architecture.md](docs/architecture.md).

| decision | why | ADR |
|---|---|---|
| supervisor + scoped workers | least privilege per step, bounded blast radius | [1](docs/adrs/0001-supervisor-topology.md) |
| custom hardened DB gateway | reference server had a documented sandbox escape | [2](docs/adrs/0002-custom-db-gateway.md) |
| Postgres checkpointer (psycopg3) | interrupts survive restarts; time travel for free | [3](docs/adrs/0003-postgres-checkpointer.md) |
| two approval mechanisms (stateful + HMAC) | matched to each server's trust boundary | [4](docs/adrs/0004-approval-verification.md) |
| no Redis | single-process bus today; seam documented for scale-out | [5](docs/adrs/0005-no-redis.md) |
| golden-trajectory evals, CI-ready but not wired | harness runs locally via `make eval`; no CI workflow yet | [6](docs/adrs/0006-eval-strategy.md) |

## Evals

Deterministic golden trajectories replay scripted model turns through the real
graph and score routing, tool selection and gate behavior. The harness is
CI-ready via `make eval` (it exits non-zero under threshold) but no CI
workflow is wired yet, so for now you run it locally.

Latest local run (`make eval`):

| metric | value | threshold |
|---|---|---|
| routing accuracy | 1.00 | ≥ 1.0 |
| tool F1 | 1.00 | ≥ 0.9 |
| gate accuracy | 1.00 | ≥ 1.0 |

Plus 137 tests (124 run without a database, 13 need Postgres): policy fuzz
cases, WS protocol, full approval round-trip against real Postgres.

## Quickstart

**Everything runs free.** No paid APIs anywhere in the stack.

```bash
cp .env.example .env
# REQUIRED before starting: generate all three secrets and put them in .env.
# The API refuses to boot without the key, and compose fails fast without
# the approval secret and the Postgres password.
export OPS_API_KEY="$(openssl rand -hex 32)"
export OPS_APPROVAL_SECRET="$(openssl rand -hex 32)"
export POSTGRES_PASSWORD="$(openssl rand -hex 32)"
docker compose up --build -d postgres
uv sync --extra dev
make seed                     # creates schemas + demo data (idempotent)

# option A: bring your own free-tier key (Groq / Gemini / OpenAI-compatible)
export OPS_LLM_PROVIDER=groq OPS_GROQ_API_KEY=gsk_...
export OPS_MCP_TRANSPORT=http

# option B: fully offline via Ollama
docker compose --profile offline up -d ollama
docker exec ops-copilot-ollama-1 ollama pull llama3.1:8b
export OPS_LLM_PROVIDER=ollama

docker compose up --build -d ops-db-mcp source-control-mcp api web
open http://localhost:3000
```

Or skip Docker entirely: `OPS_MCP_TRANSPORT=stdio` spawns both MCP servers as
subprocesses of the API.

A browser page refresh (F5) requires starting a new thread session by design,
to preserve per-thread isolation.

### Five-minute demo script

| ask the copilot | what it shows |
|---|---|
| "what's our revenue from paid orders?" | plan → analyst → fenced SQL → synthesized answer |
| "any scary open incidents?" | schema-first querying, grouped results |
| "what's happening in psf/requests lately?" | devops worker over GitHub MCP (works tokenless, rate-limited) |
| "refund order 42" | **the main event**: proposal → interrupt → SQL shown → approve/reject → audited execution or clean rejection |
| same again after `docker compose restart api` | pending approval survived the restart (Postgres checkpointer) |

`GET /api/threads/{id}/history` lists checkpoints;
`POST /api/threads/{id}/replay {"checkpoint_id": ...}` forks execution from any
of them.

### Local Debugging & Inspection

The MCP ports are intentionally unpublished, so inspect the database through
compose exec instead of a host port:

```bash
docker compose exec postgres psql -U ops -d ops_ops
docker compose exec postgres psql -U ops -d ops_checkpoints
```

## Stack

Python 3.11 · LangGraph 1.x (`interrupt` / `Command` resume) · FastMCP ·
langchain-mcp-adapters · LiteLLM (Groq/Gemini/Ollama/openai-compatible) · psycopg3 + AsyncPostgresSaver · FastAPI +
WebSockets · Next.js 14 + Tailwind · Docker Compose ·
structlog. Observability hooks are env-gated (Phoenix endpoint, LangSmith) so
nothing requires an account to run.

Dependency locking: run `uv lock` on the dev machine and switch the Dockerfile
back to `uv sync --frozen` when locking dependencies.

## Repository layout

```
src/ops_copilot/
  graph/        state, supervisor, workers, gate+executor, builder
  mcp/servers/  ops_db.py (SQL fence), source_control.py (GitHub)
  mcp/client.py role-based tool loading (worker RBAC)
  guardrails.py SQL policy engine, PII masking, token budgets
  security.py   HMAC approval tokens
  api/          FastAPI, event bus, graph runner, approvals
evals/          golden-trajectory harness + judge
tests/          unit + integration (real Postgres round-trips)
web/            Next.js console (streaming chat, approval cards, task plan)
docs/adrs/      six decision records
```

## Known limitations

Stated plainly rather than hidden ([more](docs/adrs/0006-eval-strategy.md)):

- Live-model evals aren't in CI; golden trajectories prove control flow, not
  prompt quality against real providers.
- Single-node event bus; horizontal scale needs Redis/NATS behind one class.
- Triage drafts never leave the system (no email/Slack provider); sending would
  be another HMAC-gated side effect.
- GitHub tools degrade gracefully but meaningfully when anonymous (60 req/h).
- The SQL policy parser is regex-and-classification, not a full grammar; it is
  deliberately conservative and layered under driver-level read-only anyway.
- The audit log is hash-chained local JSONL: tamper-evident against
  append-only attackers, but not tamper-proof against someone who can rewrite
  the file.
- Approval records name whoever the caller claims to be: with a single shared
  API key there is no per-user identity, so `decided_by` is caller-asserted
  and only as trustworthy as the key itself.
- The compose file ships one database role for everything. For a real
  deployment, give the app DSNs a least-privilege role (no CREATEDB, no
  superuser) and reserve the privileged account for `scripts/bootstrap_db.py`,
  which needs CREATEDB to create the databases.
- Auth is one shared API key plus per-thread ownership tokens (returned at
  thread creation, required as X-Thread-Token / the x-thread-token WebSocket
  subprotocol). There is no user or principal concept: anyone holding the key
  can create threads, and anyone holding a thread's token can drive it. Full
  multi-user auth is out of scope for this pass.
- The single-use approval-token ledger lives in process memory, so an API
  restart inside the 15-minute token window resets it. Primary defenses
  remain token non-exposure, TTL plus target/content binding plus signature,
  and the replay guard that refuses re-execution past the approval gate.

## License

MIT
