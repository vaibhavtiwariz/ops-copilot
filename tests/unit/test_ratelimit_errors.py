"""Unit tests for the S7 rate limiter and the S13 error allowlist."""

from ops_copilot.api.errors import public_error
from ops_copilot.api.ratelimit import RateLimiter


def test_rate_limiter_allows_up_to_limit_then_denies():
    limiter = RateLimiter()
    assert all(limiter.allow("key-1", "thread_create", 3) for _ in range(3))
    assert not limiter.allow("key-1", "thread_create", 3)


def test_rate_limiter_is_per_key_and_per_bucket():
    limiter = RateLimiter()
    assert limiter.allow("key-1", "thread_create", 1)
    assert not limiter.allow("key-1", "thread_create", 1)
    # a different key and a different bucket are unaffected
    assert limiter.allow("key-2", "thread_create", 1)
    assert limiter.allow("key-1", "replay", 1)


def test_public_error_passes_safe_types_through():
    assert public_error(TimeoutError("upstream timed out")) == "upstream timed out"

    class ThreadBusy(RuntimeError):
        pass

    assert public_error(ThreadBusy("a run is already active")) == "a run is already active"


def test_public_error_hides_everything_else():
    assert (
        public_error(RuntimeError("select * from users blew up at /srv/db.py:12"))
        == "internal error; check server logs"
    )
    assert public_error(ValueError("weird")) == "internal error; check server logs"
