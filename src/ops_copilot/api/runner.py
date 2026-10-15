"""Runs the graph and translates raw stream chunks into UI-facing events.

Both entry points funnel through here: a websocket user turn (input =
messages) and an approval resume (input = Command(resume=...)).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from ops_copilot.api.errors import public_error
from ops_copilot.api.events import EventBus
from ops_copilot.graph.build import runnable_config
from ops_copilot.llm import rate_limit_sink
from ops_copilot.logging import get_logger

log = get_logger(__name__)


class ThreadBusy(RuntimeError):
    pass


class GraphRunner:
    def __init__(self, graph: Any, bus: EventBus) -> None:
        self.graph = graph
        self.bus = bus
        self._locks: dict[str, asyncio.Lock] = {}

    _MAX_LOCKS = 1000

    def _lock(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._locks:
            if len(self._locks) >= self._MAX_LOCKS:
                # One lock per thread is fine until threads number in the
                # thousands; then evict the oldest entries whose lock is not
                # currently held so the dict cannot grow forever.
                for old_id, old_lock in list(self._locks.items()):
                    if not old_lock.locked():
                        del self._locks[old_id]
                    if len(self._locks) < self._MAX_LOCKS:
                        break
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]

    def is_busy(self, thread_id: str) -> bool:
        """True while a run (turn or approval resume) holds this thread's lock."""
        lock = self._locks.get(thread_id)
        return lock.locked() if lock is not None else False

    async def run_turn(
        self,
        thread_id: str,
        content: str,
        *,
        resume_value: dict | None = None,
        replay_from: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield events for one full graph run; caller forwards them to sockets."""
        lock = self._lock(thread_id)
        if lock.locked():
            raise ThreadBusy("a run is already active on this thread")

        config = runnable_config(thread_id)
        if replay_from:
            config["configurable"]["checkpoint_id"] = replay_from

        if resume_value is not None:
            payload: Any = Command(resume=resume_value)
        elif replay_from:
            payload = None  # re-execute from the chosen checkpoint
        else:
            payload = {"messages": [HumanMessage(content=content)]}

        async with lock:
            await self.bus.publish(thread_id, {"type": "run_started"})
            # Rate-limit waits inside the graph surface as console events.
            sink_token = rate_limit_sink.set((self.bus, thread_id))
            try:
                async for mode, chunk in self.graph.astream(
                    payload, config, stream_mode=["updates", "messages"]
                ):
                    event = _translate(mode, chunk)
                    if event:
                        # An approval resume has no direct consumer (_drain
                        # discards the generator), so its tokens only reach
                        # the console via the bus. Normal turns keep tokens
                        # generator-only to avoid duplicating them through
                        # the WS pump. Replay uses replay_from, not
                        # resume_value, so it is unaffected.
                        if resume_value is not None and event["type"] in ("token", "node"):
                            await self.bus.publish(thread_id, event)
                        yield event
                # A pinned checkpoint_id describes where the run started, not
                # what it produced. Strip it so the post-run state (and any
                # interrupt/done events) describe this run's fresh checkpoint.
                config["configurable"].pop("checkpoint_id", None)
                state = await self.graph.aget_state(config)
                interrupts = _collect_interrupts(state)
                if interrupts:
                    for item in interrupts:
                        await self.bus.publish(thread_id, {"type": "interrupt", **item})
                    yield {"type": "interrupt", "interrupts": interrupts}
                await self.bus.publish(thread_id, {"type": "done"})
                yield {"type": "done"}
            except Exception as exc:
                log.error("turn_failed", thread_id=thread_id, error=str(exc), exc_info=True)
                # S13: raw exception text can carry SQL fragments or provider
                # internals; clients get the allowlisted shape only.
                message = f"run failed: {public_error(exc)}"
                await self.bus.publish(thread_id, {"type": "error", "message": message})
                yield {"type": "error", "message": message}
            finally:
                rate_limit_sink.reset(sink_token)


def _translate(mode: str, chunk: Any) -> dict | None:
    if mode == "messages":
        message_chunk, metadata = chunk
        node = (metadata or {}).get("langgraph_node")
        # The planner's raw JSON plan is internal plumbing - users see its
        # effect as delegated tasks, not as chat text.
        if node == "planner":
            return None
        if isinstance(message_chunk, AIMessage) and message_chunk.content:
            text = _chunk_text(message_chunk.content)
            if text:
                return {"type": "token", "node": node, "content": text}
        return None
    if mode == "updates":
        if isinstance(chunk, dict):
            if "__interrupt__" in chunk:
                return None  # handled after the loop from checkpoint state
            nodes = list(chunk.keys())
            if nodes:
                return {"type": "node", "nodes": nodes}
        return None
    return None


def _chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _collect_interrupts(state: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in getattr(state, "tasks", None) or []:
        for interrupt in getattr(task, "interrupts", None) or []:
            value = getattr(interrupt, "value", None)
            out.append(
                {
                    "id": str(getattr(interrupt, "id", "")),
                    "payload": value if isinstance(value, dict) else {"raw": value},
                }
            )
    return out


def pending_payload(state: Any) -> dict[str, Any] | None:
    """The proposal awaiting review, straight from checkpoint state."""
    interrupts = _collect_interrupts(state)
    if interrupts:
        return interrupts[-1]["payload"]
    awaiting = (
        getattr(state, "values", {}).get("awaiting") if getattr(state, "values", None) else None
    )
    return awaiting if isinstance(awaiting, dict) and awaiting else None
