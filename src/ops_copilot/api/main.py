"""FastAPI application: threads, websocket streaming, approvals, time travel."""

import asyncio
import hashlib
import hmac
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ops_copilot.api import schemas
from ops_copilot.api.errors import public_error
from ops_copilot.api.events import EventBus
from ops_copilot.api.ratelimit import (
    REPLAY_PER_MINUTE,
    THREAD_CREATE_PER_MINUTE,
    THREAD_LIST_PER_MINUTE,
    WS_IDLE_TIMEOUT_S,
    WS_MAX_CONNECTIONS,
    RateLimiter,
)
from ops_copilot.api.runner import GraphRunner, ThreadBusy, pending_payload
from ops_copilot.audit import AuditLog, load_audit
from ops_copilot.graph.build import build_graph
from ops_copilot.logging import configure_logging, get_logger
from ops_copilot.settings import Settings, get_settings

log = get_logger("api")

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    if not settings.api_key:
        raise RuntimeError(
            "OPS_API_KEY is not set. Generate one with `openssl rand -hex 32` "
            "and export OPS_API_KEY before starting the API."
        )
    if not os.environ.get("POSTGRES_PASSWORD"):
        raise RuntimeError(
            "POSTGRES_PASSWORD is not set. Export POSTGRES_PASSWORD with the "
            "database password (and reference it in your database URLs) before "
            "starting the API."
        )
    app.state.bus = EventBus()
    app.state.audit = AuditLog(settings.audit_log_path)

    if getattr(app.state, "graph", None) is None:
        checkpoint_pool = AsyncConnectionPool(
            settings.checkpoint_db_url,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        await checkpoint_pool.open(wait=True, timeout=15)
        checkpointer = await _make_checkpointer(checkpoint_pool)
        app.state.checkpoint_pool = checkpoint_pool
        app.state.graph = build_graph(checkpointer)
        log.info("checkpointer_ready", backend="postgres")

    app.state.runner = GraphRunner(app.state.graph, app.state.bus)

    if getattr(app.state, "app_pool", None) is None:
        try:
            app_pool: AsyncConnectionPool = AsyncConnectionPool(
                settings.database_url,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=False,
            )
            await app_pool.open(wait=False, timeout=10)
            app.state.app_pool = app_pool
        except Exception as exc:
            log.error("app_db_unavailable", error=str(exc))
            app.state.app_pool = None

    yield

    for attr in ("checkpoint_pool", "app_pool"):
        pool: Any = getattr(app.state, attr, None)
        if pool is not None:
            await pool.close()


async def _make_checkpointer(pool: AsyncConnectionPool) -> BaseCheckpointSaver:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    # Task objects live inside checkpoints; without this registration every
    # deserialization logs a deprecation warning and future versions will
    # refuse them outright.
    serde = JsonPlusSerializer(
        allowed_json_modules=[("ops_copilot.graph",)],
        allowed_msgpack_modules=[("ops_copilot.graph.state", "Task")],
    )
    saver = AsyncPostgresSaver(pool, serde=serde)  # type: ignore[arg-type]
    # Creates/migrates the checkpoints tables; safe to call on every boot.
    await saver.setup()
    return saver


def create_app(*, graph=None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="ops-copilot", version=VERSION, lifespan=lifespan)
    if graph is not None:
        app.state.graph = graph
    if settings is not None:
        app.state.settings = settings

    # The console runs on a different origin; without this every browser
    # request dies at preflight while curl works fine.
    cors = get_settings().cors_origin_list
    if cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    api = APIRouter(prefix="/api")

    @api.post("/threads", response_model=schemas.ThreadCreated)
    async def create_thread(request: Request, x_api_key: str | None = Header(default=None)):
        _check_key(request, x_api_key)
        _check_rate_limit(request, x_api_key, "thread_create", THREAD_CREATE_PER_MINUTE)
        thread_id = uuid.uuid4().hex[:12]
        thread_token = await _issue_thread_token(request, thread_id)
        return schemas.ThreadCreated(thread_id=thread_id, thread_token=thread_token)

    @api.get("/threads", response_model=list[schemas.ThreadListItem])
    async def list_threads(request: Request, x_api_key: str | None = Header(default=None)):
        """Completed threads for the console history panel.

        History is for finished work: a thread appears only once its run
        is at rest with nothing awaiting and every task in a terminal
        state (or a completed direct reply with no tasks). In-progress,
        awaiting, and never-used threads do not list. No content is
        returned, so a thread stays locked behind its token even though
        its id is listed.
        """
        _check_key(request, x_api_key)
        _check_rate_limit(request, x_api_key, "thread_list", THREAD_LIST_PER_MINUTE)
        pool = getattr(request.app.state, "app_pool", None)
        if pool is None:
            raise HTTPException(status_code=503, detail="thread store unavailable")
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    "select thread_id, created_at from thread_tokens "
                    "order by created_at desc limit 50"
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            log.error("thread_list_failed", error=str(exc))
            raise HTTPException(status_code=503, detail="thread store unavailable") from exc
        runner = request.app.state.runner
        items: list[schemas.ThreadListItem] = []
        for row in rows:
            thread_id = row["thread_id"]
            try:
                snapshot = await runner.graph.aget_state({"configurable": {"thread_id": thread_id}})
            except Exception:
                continue
            if getattr(snapshot, "next", None):
                continue  # mid-run or paused at the gate: not finished work
            if pending_payload(snapshot) is not None:
                continue  # approval still outstanding: not finished work
            values = getattr(snapshot, "values", None) or {}
            tasks = values.get("tasks") or []
            message_count = len(values.get("messages") or [])
            if tasks:
                if any(t.status not in ("done", "failed", "rejected") for t in tasks):
                    continue
                status = (
                    "rejected" if any(t.status in ("failed", "rejected") for t in tasks) else "done"
                )
            elif message_count >= 2:
                status = "done"
            else:
                continue  # created but never used: no finished work
            last_active = getattr(snapshot, "created_at", None) or row["created_at"]
            items.append(
                schemas.ThreadListItem(
                    thread_id=thread_id,
                    created_at=last_active,
                    status=status,
                    message_count=message_count,
                )
            )
        items.sort(
            key=lambda item: item.created_at.isoformat() if item.created_at else "", reverse=True
        )
        return items

    @api.get("/threads/{thread_id}/state", response_model=schemas.StateView)
    async def thread_state(
        thread_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        runner = request.app.state.runner
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await runner.graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        tasks = values.get("tasks") or []
        awaiting = pending_payload(snapshot)
        if awaiting:
            # S11: the approval token lives in `awaiting` between approval and
            # execution; any API caller could otherwise lift a live token.
            awaiting = {k: v for k, v in awaiting.items() if k != "approval_token"}
        return schemas.StateView(
            thread_id=thread_id,
            tasks=[
                schemas.TaskView(
                    id=t.id,
                    description=t.description,
                    assignee=t.assignee,
                    status=t.status,
                    summary=t.summary,
                )
                for t in tasks
            ],
            awaiting=awaiting,
            message_count=len(values.get("messages") or []),
            error=values.get("error"),
        )

    @api.get("/threads/{thread_id}/transcript", response_model=list[schemas.TranscriptMessage])
    async def thread_transcript(
        thread_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        """The thread's chat transcript, for reopening and as a WS backstop.

        Token-gated like /state (S4). Human and assistant text only: tool
        messages are omitted, and assistant turns with no text (tool-call
        only) are skipped. The console fetches this on thread open (so a
        reopened thread shows its real conversation) and on done (so a run
        whose WS tokens were missed still lands its answer).
        """
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        runner = request.app.state.runner
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await runner.graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        out: list[schemas.TranscriptMessage] = []
        for message in values.get("messages") or []:
            content = getattr(message, "content", "")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            if not isinstance(content, str) or not content.strip():
                continue
            if isinstance(message, HumanMessage):
                out.append(schemas.TranscriptMessage(role="user", content=content))
            elif isinstance(message, AIMessage):
                if content.startswith("[guardrail]"):
                    continue  # internal plumbing, not conversation
                out.append(schemas.TranscriptMessage(role="assistant", content=content))
        return out

    @api.get("/threads/{thread_id}/history", response_model=list[schemas.CheckpointView])
    async def thread_history(
        thread_id: str,
        request: Request,
        limit: int = 25,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        runner = request.app.state.runner
        config = {"configurable": {"thread_id": thread_id}}
        history = []
        async for snap in runner.graph.aget_state_history(config, limit=min(limit, 100)):
            history.append(
                schemas.CheckpointView(
                    checkpoint_id=snap.config["configurable"]["checkpoint_id"],
                    step=(snap.metadata or {}).get("step", 0),
                    created_at=getattr(snap, "created_at", None),
                    next_nodes=[str(n) for n in (snap.next or ())],
                )
            )
        return history

    @api.post("/threads/{thread_id}/replay")
    async def replay(
        thread_id: str,
        body: schemas.ReplayRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        """Fork execution from an earlier checkpoint - the time-travel demo."""
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        _check_rate_limit(request, x_api_key, "replay", REPLAY_PER_MINUTE)
        runner = request.app.state.runner
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": body.checkpoint_id,
            }
        }
        snapshot = await runner.graph.aget_state(config)
        if snapshot.next and "executor" in snapshot.next:
            raise HTTPException(
                status_code=409,
                detail="cannot replay from a checkpoint past the approval gate; "
                "re-running it could repeat real-world side effects",
            )
        tail: list[dict] = []
        async for event in runner.run_turn(thread_id, content="", replay_from=body.checkpoint_id):
            tail.append(event)
        return {"thread_id": thread_id, "event_count": len(tail), "events": tail[-20:]}

    @api.post("/threads/{thread_id}/approvals", response_model=schemas.ApprovalAck)
    async def decide_approval(
        thread_id: str,
        body: schemas.ApprovalRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        runner = request.app.state.runner

        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await runner.graph.aget_state(config)
        payload = pending_payload(snapshot)
        if payload is None:
            raise HTTPException(
                status_code=409, detail="nothing is waiting for approval on this thread"
            )

        if payload.get("kind") == "db_write" and body.approved:
            ok = await _record_db_approval(request, int(payload["proposal_id"]), body)
            if not ok:
                raise HTTPException(
                    status_code=503,
                    detail="approval table unavailable; refusing to resume unsafely",
                )
            request.app.state.audit.emit(
                "approval",
                actor=body.decided_by,
                summary=f"approved proposal {payload['proposal_id']}",
                detail={
                    "proposal_id": payload["proposal_id"],
                    "note": body.note,
                    "thread_id": thread_id,
                },
            )
        else:
            request.app.state.audit.emit(
                "rejection" if not body.approved else "approval",
                actor=body.decided_by,
                summary=payload.get("summary", "decision recorded"),
                detail={"payload": payload, "note": body.note, "thread_id": thread_id},
            )

        decision = {"approved": body.approved, "note": body.note, "decided_by": body.decided_by}
        try:
            await asyncio.wait_for(_drain(runner, thread_id, decision), timeout=120)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="resume started but did not finish in time; check the thread state",
            ) from exc
        except ThreadBusy as exc:
            raise HTTPException(
                status_code=409, detail="thread is busy; a resume is already running"
            ) from exc
        except Exception as exc:
            # S13: never leak raw exception text to the client.
            raise HTTPException(
                status_code=502, detail=f"resume failed: {public_error(exc)}"
            ) from exc
        return schemas.ApprovalAck(thread_id=thread_id, accepted=True, detail="resumed")

    @api.get(
        "/threads/{thread_id}/proposals/{proposal_id}",
        response_model=schemas.ProposalView,
    )
    async def thread_proposal(
        thread_id: str,
        proposal_id: int,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        """Return the exact SQL of a registered proposal for the human reviewer.

        S6: the proposal SQL lives here and in the proposals table only. The
        interrupt payload, checkpoints, audit records, and /state carry
        proposal_id + summary + action/table instead.

        Limitation: proposals are not linked to threads, so this endpoint
        cannot verify that the proposal belongs to the given thread; any
        holder of a valid thread token can read any proposal's SQL.
        """
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        pool = getattr(request.app.state, "app_pool", None)
        if pool is None:
            raise HTTPException(status_code=503, detail="proposal store unavailable")
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    "select id, action, table_name, sql_text, status from proposals where id = %s",
                    (proposal_id,),
                )
                row = await cursor.fetchone()
        except Exception as exc:
            log.error("proposal_lookup_failed", proposal_id=proposal_id, error=str(exc))
            raise HTTPException(status_code=503, detail="proposal store unavailable") from exc
        if row is None:
            raise HTTPException(status_code=404, detail="no such proposal")
        return schemas.ProposalView(
            proposal_id=row["id"],
            action=row["action"],
            table=row["table_name"],
            sql=row["sql_text"],
            status=row["status"],
        )

    @api.get("/threads/{thread_id}/audit", response_model=list[schemas.AuditRecord])
    async def thread_audit(
        thread_id: str,
        request: Request,
        limit: int = 50,
        x_api_key: str | None = Header(default=None),
        x_thread_token: str | None = Header(default=None),
    ):
        """Audit records attributable to this thread.

        Approval and rejection records carry the thread id in their detail.
        (The executor's apply records do not carry it, so they stay on the
        global /audit endpoint.) Older or system records without one are not
        attributable and stay on the global /audit endpoint.
        """
        _check_key(request, x_api_key)
        await _check_thread_token(request, thread_id, x_thread_token)
        records = load_audit(get_settings().audit_log_path, limit=min(limit, 500))
        mine = [r for r in records if (r.get("detail") or {}).get("thread_id") == thread_id]
        return [schemas.AuditRecord(**r) for r in mine]

    @api.get("/audit", response_model=list[schemas.AuditRecord])
    async def recent_audit(
        request: Request, limit: int = 50, x_api_key: str | None = Header(default=None)
    ):
        _check_key(request, x_api_key)
        records = load_audit(get_settings().audit_log_path, limit=min(limit, 500))
        return [schemas.AuditRecord(**r) for r in records]

    @api.get("/healthz")
    async def healthz():
        return {"ok": True}

    @api.get("/readyz", response_model=schemas.HealthReport)
    async def readyz(request: Request):
        app_db_ok = await _pool_ok(request, "app_pool")
        cp_ok = await _pool_ok(request, "checkpoint_pool")
        status = "ready" if (app_db_ok and cp_ok) else "degraded"
        return schemas.HealthReport(
            status=status, app_db=app_db_ok, checkpoint_db=cp_ok, version=VERSION
        )

    app.include_router(api)

    # S7: in-memory rate limiting for the expensive surfaces, and the global
    # WebSocket connection cap. Set here (not in lifespan) so tests that
    # build the app without running lifespan still get them.
    app.state.limiter = RateLimiter()
    app.state.ws_slots = asyncio.Semaphore(WS_MAX_CONNECTIONS)

    @app.websocket("/ws/{thread_id}")
    async def stream(socket: WebSocket, thread_id: str):
        await _websocket_session(socket, thread_id)

    return app


def _check_key(request: Request, provided: str | None) -> None:
    settings: Settings = request.app.state.settings
    if settings.api_key and not hmac.compare_digest(provided or "", settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _check_rate_limit(request: Request, api_key: str | None, bucket: str, limit: int) -> None:
    """S7: 429 when one API key hammers an expensive endpoint."""
    limiter: RateLimiter = request.app.state.limiter
    if not limiter.allow(api_key or "", bucket, limit):
        raise HTTPException(status_code=429, detail=f"rate limit exceeded for {bucket}; slow down")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _issue_thread_token(request: Request, thread_id: str) -> str:
    """Mint a per-thread ownership token; only its sha256 is stored (S4).

    The token is returned once at thread creation. Every thread-scoped
    endpoint and the WebSocket demand it back, so one API key cannot drive
    another thread.
    """
    pool = getattr(request.app.state, "app_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="thread token store unavailable")
    token = secrets.token_hex(32)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "insert into thread_tokens (thread_id, token_hash) values (%s, %s) "
                "on conflict (thread_id) do update "
                "set token_hash = excluded.token_hash, created_at = now()",
                (thread_id, _token_hash(token)),
            )
    except Exception as exc:
        log.error("thread_token_store_failed", thread_id=thread_id, error=str(exc))
        raise HTTPException(status_code=503, detail="thread token store unavailable") from exc
    return token


async def _lookup_token_hash(pool: Any, thread_id: str) -> str:
    """Fetch the stored sha256 for a thread id, "" when unknown."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "select token_hash from thread_tokens where thread_id = %s",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return row["token_hash"] if row else ""


async def _check_thread_token(request: Request, thread_id: str, provided: str | None) -> None:
    """S4: 403 unless the caller presents this thread's creation token."""
    pool = getattr(request.app.state, "app_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="thread token store unavailable")
    try:
        stored = await _lookup_token_hash(pool, thread_id)
    except Exception as exc:
        log.error("thread_token_lookup_failed", thread_id=thread_id, error=str(exc))
        raise HTTPException(status_code=503, detail="thread token store unavailable") from exc
    # Unknown thread ids hash against "" and fail the same way, so the
    # response never reveals whether a thread exists.
    if not hmac.compare_digest(_token_hash(provided or ""), stored):
        raise HTTPException(status_code=403, detail="invalid or missing X-Thread-Token")


async def _check_thread_token_ws(socket: WebSocket, thread_id: str, offered: list[str]) -> bool:
    """WebSocket flavor of _check_thread_token; True when the token verifies."""
    presented = next(
        (p[len("x-thread-token.") :] for p in offered if p.startswith("x-thread-token.")),
        "",
    )
    pool = getattr(socket.app.state, "app_pool", None)
    if pool is None:
        return False
    try:
        stored = await _lookup_token_hash(pool, thread_id)
    except Exception as exc:
        log.error("thread_token_lookup_failed", thread_id=thread_id, error=str(exc))
        return False
    return bool(stored) and hmac.compare_digest(_token_hash(presented), stored)


async def _record_db_approval(
    request: Request, proposal_id: int, body: schemas.ApprovalRequest
) -> bool:
    """Flip the proposals row so the gateway will accept the apply call.

    Idempotent: a double-clicked approve reuses the existing approvals row
    instead of inserting a duplicate (backed by the unique constraint on
    approvals.proposal_id).
    """
    pool = getattr(request.app.state, "app_pool", None)
    if pool is None:
        return False
    async with pool.connection() as conn:
        existing = await conn.execute(
            "select id from approvals where proposal_id = %s", (proposal_id,)
        )
        if (await existing.fetchone()) is None:
            await conn.execute(
                """
                insert into approvals (proposal_id, approved, note, decided_by)
                values (%s, %s, %s, %s)
                """,
                (proposal_id, body.approved, body.note, body.decided_by),
            )
        await conn.execute(
            "update proposals set status = 'approved' where id = %s and status = 'pending'",
            (proposal_id,),
        )
    return True


async def _drain(runner: GraphRunner, thread_id: str, decision: dict) -> None:
    try:
        async for _ in runner.run_turn(thread_id, "", resume_value=decision):
            pass
    except Exception:
        log.exception("resume_run_failed", thread_id=thread_id)
        raise


async def _pool_ok(request: Request, attr: str) -> bool:
    pool = getattr(request.app.state, attr, None)
    if pool is None:
        return False
    try:
        async with pool.connection() as conn:
            await conn.execute("select 1")
        return True
    except Exception:
        return False


async def _websocket_session(socket: WebSocket, thread_id: str) -> None:
    settings: Settings = socket.app.state.settings
    offered = [
        part.strip()
        for part in (socket.headers.get("sec-websocket-protocol") or "").split(",")
        if part.strip()
    ]
    if settings.api_key:
        # Browsers cannot set headers on the WebSocket handshake, so the
        # console offers the key as a subprotocol token "x-api-key.<key>".
        presented = next(
            (p[len("x-api-key.") :] for p in offered if p.startswith("x-api-key.")),
            "",
        )
        if not hmac.compare_digest(presented, settings.api_key):
            await socket.close(code=4401)
            return

    # S4: per-thread ownership on the socket too; the creation-time token
    # travels as "x-thread-token.<tok>" alongside the key subprotocol.
    if not await _check_thread_token_ws(socket, thread_id, offered):
        await socket.close(code=4403)
        return

    # S7: global connection cap; each socket pins a queue, a pump task, and
    # a bus subscription, so unbounded sockets are a memory/LLM-spend hole.
    slots: asyncio.Semaphore = socket.app.state.ws_slots
    if slots.locked():
        await socket.close(code=4429)
        return
    await slots.acquire()

    await socket.accept(subprotocol="copilot")
    bus: EventBus = socket.app.state.bus
    runner: GraphRunner = socket.app.state.runner
    queue = await bus.subscribe(thread_id)

    async def pump() -> None:
        while True:
            event = await queue.get()
            await socket.send_json(event)

    pumping = asyncio.create_task(pump())
    await socket.send_json({"type": "connected", "thread_id": thread_id})
    try:
        while True:
            try:
                # S7: idle sockets (no inbound frame for 10 minutes) are
                # closed instead of lingering with their queue and pump task.
                # Exception: a run in flight (for example an approval resume
                # driven by the HTTP endpoint, not by this socket) still
                # needs this socket for its done event, so keep waiting.
                inbound = await asyncio.wait_for(socket.receive_json(), timeout=WS_IDLE_TIMEOUT_S)
            except TimeoutError:
                if runner.is_busy(thread_id):
                    continue
                log.info("ws_idle_timeout", thread_id=thread_id)
                await socket.close(code=4408)
                return
            except ValueError:
                # S19: malformed JSON must not kill the session with 1011.
                await socket.send_json(
                    {
                        "type": "error",
                        "message": "expected a JSON object frame",
                        "detail": "expected a JSON object frame",
                    }
                )
                continue
            # S19: non-dict frames (arrays, strings) have no .get; answer
            # with an error event instead of dying with 1011.
            if not isinstance(inbound, dict):
                await socket.send_json(
                    {
                        "type": "error",
                        "message": "expected a JSON object frame",
                        "detail": "expected a JSON object frame",
                    }
                )
                continue
            kind = inbound.get("type")
            if kind == "user_message":
                content = str(inbound.get("content", "")).strip()
                if not content:
                    await socket.send_json({"type": "error", "message": "empty message"})
                    continue
                try:
                    async for event in runner.run_turn(thread_id, content):
                        await socket.send_json(event)
                except ThreadBusy as exc:
                    await socket.send_json({"type": "error", "message": str(exc)})
            elif kind == "ping":
                await socket.send_json({"type": "pong"})
            else:
                await socket.send_json({"type": "error", "message": f"unknown frame type {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        slots.release()
        pumping.cancel()
        await bus.unsubscribe(thread_id, queue)


app = create_app()
