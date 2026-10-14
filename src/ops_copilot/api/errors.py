"""Shape exceptions for client-facing surfaces.

Raw str(exc) can carry SQL fragments, file paths, or provider internals, so
only exception types a client can act on keep their message; everything else
becomes a generic pointer at the server logs. Used by the WS session, the
runner's error events, the supervisor's planner-failure message, and the
approval endpoint's failure detail.
"""

import asyncio

# Messages safe to show: the client can act on these (slow down, retry
# later). ThreadBusy lives in api.runner; matched by name to avoid an import
# cycle (runner imports this module).
_SAFE_TYPES = (asyncio.TimeoutError, TimeoutError)
_SAFE_NAMES = frozenset({"ThreadBusy"})

_GENERIC = "internal error; check server logs"


def public_error(exc: BaseException) -> str:
    """Return a client-safe error string for an exception."""
    if isinstance(exc, _SAFE_TYPES) or type(exc).__name__ in _SAFE_NAMES:
        return str(exc)
    return _GENERIC
