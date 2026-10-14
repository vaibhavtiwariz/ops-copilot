# 5. No Redis

**Status:** accepted

The original design sketch included Redis for "event streaming and caching".
On inspection both jobs had cheaper, more honest solutions:

- **Event streaming.** The API is a single process in every deployment we
  actually run. An in-process fan-out bus (`api/events.py`, one asyncio.Queue
  per websocket subscriber) does the same thing with no network hop and no
  second datastore to operate. The class interface was written so that
  swapping in Redis pub/sub later touches exactly one file - that is the
  scaling seam, documented rather than pre-built.
- **Caching.** Only the GitHub server needed caching (rate limits), and a TTL
  dict inside the server process covers it.

Running Redis because a production system eventually might need it is exactly
the kind of resume-driven architecture this project tries to avoid. The ADR
exists so the decision is visible, not forgotten.
