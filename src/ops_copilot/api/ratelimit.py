"""In-memory rate limiting for the API's expensive surfaces.

Kept deliberately small: a single process is the deployment unit (the
checkpointer and the event bus already assume one), so per-process counters
are honest here. If the API ever scales horizontally, replace RateLimiter
with a Redis-backed counter behind the same allow() signature.

Constants, documented because they are the policy:
- THREAD_CREATE_PER_MINUTE = 20: each thread fans out to full LLM graph
  runs, so thread creation itself is throttled per API key.
- REPLAY_PER_MINUTE = 10: replay re-executes graph turns from checkpoints,
  each one billable model work.
- WS_MAX_CONNECTIONS = 100: global cap; every socket pins a queue, a pump
  task, and a bus subscription.
- WS_IDLE_TIMEOUT_S = 600: sockets with no inbound frame for 10 minutes are
  closed (code 4408) instead of lingering forever.
- WS_QUEUE_MAXSIZE = 512: per-subscriber event queue bound (see
  api/events.py EventBus.subscribe); overflow drops with a warning instead
  of growing memory without bound.
"""

import hashlib
import threading
import time
from collections import deque

THREAD_CREATE_PER_MINUTE = 20
REPLAY_PER_MINUTE = 10
WS_MAX_CONNECTIONS = 100
WS_IDLE_TIMEOUT_S = 600
WS_QUEUE_MAXSIZE = 512

_WINDOW_S = 60
_MAX_BUCKETS = 4096


class RateLimiter:
    """Sliding-window per-key counter; allow() is False when over budget."""

    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, api_key: str, bucket: str, limit: int) -> bool:
        # The raw key never leaves this process; the dict key is its hash.
        key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cutoff = now - _WINDOW_S
        with self._lock:
            window = self._windows.setdefault((key, bucket), deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            if len(self._windows) > _MAX_BUCKETS:
                # Bound memory: drop idle windows.
                stale = [k for k, w in self._windows.items() if not w or w[-1] <= cutoff]
                for k in stale:
                    del self._windows[k]
            return True
