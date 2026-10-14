"""In-process fan-out of graph events to websocket subscribers.

One queue per subscriber; the graph runner publishes, every connected socket
for that thread receives. Single-node deployments only - if this service ever
scales horizontally, this class is the seam to replace with Redis pub/sub or
NATS without touching anything above it.
"""

import asyncio

from ops_copilot.api.ratelimit import WS_QUEUE_MAXSIZE
from ops_copilot.logging import get_logger

log = get_logger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, thread_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.setdefault(thread_id, set()).add(queue)
        return queue

    async def unsubscribe(self, thread_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(thread_id)
            if subscribers:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(thread_id, None)

    async def publish(self, thread_id: str, event: dict) -> None:
        async with self._lock:
            targets = list(self._subscribers.get(thread_id, ()))
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event_queue_overflow", thread_id=thread_id)
