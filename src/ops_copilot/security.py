"""HMAC approval tokens for side effects outside the database trust boundary.

The database gateway can verify approvals against its own tables. The GitHub
server is deliberately stateless, so when a human approves a comment the API
mints a short-lived signed token that binds the exact target and content hash;
the server refuses to post without one it can verify.
"""

import hashlib
import hmac
import json
import time
import uuid


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def mint(*, secret: str, action: str, target: str, content: str, ttl_seconds: int = 900) -> str:
    body = {
        "action": action,
        "target": target,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "exp": int(time.time()) + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return f"{raw}.{_sign(raw.encode(), secret)}"


def verify(*, secret: str, token: str, action: str, target: str, content: str) -> tuple[bool, str]:
    try:
        raw, signature = token.rsplit(".", 1)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return False, "malformed approval token"

    expected = _sign(raw.encode(), secret)
    if not hmac.compare_digest(signature, expected):
        return False, "bad signature"
    if claims.get("action") != action:
        return False, f"token was issued for action {claims.get('action')!r}"
    if claims.get("target") != target:
        return False, "token does not cover this target"
    if claims.get("content_sha256") != hashlib.sha256(content.encode()).hexdigest():
        return False, "content differs from the approved draft"
    if int(claims.get("exp", 0)) < time.time():
        return False, "approval token expired"
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        # Pre-fix tokens had no jti and can never be single-use, so treat them
        # as invalid. They only lived 15 minutes anyway.
        return False, "approval token has no valid jti"
    return True, "ok"
