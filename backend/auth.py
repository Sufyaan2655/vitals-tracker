"""Password hashing + signed session cookies - no external auth dependency.

Kept to the standard library on purpose: PBKDF2-HMAC-SHA256 for password
hashing (secrets.compare_digest for constant-time verification) and HMAC-
signed, expiring tokens for sessions, instead of pulling in passlib/PyJWT
for a personal-scale app. The signing secret is generated once and persisted
locally (gitignored) so sessions survive a server restart but the key is
never committed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

PBKDF2_ITERATIONS = 200_000
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
_SECRET_KEY_PATH = Path(__file__).parent / ".session_secret"


def _get_secret_key() -> bytes:
    # A serverless deployment's filesystem doesn't persist between
    # invocations (Vercel, most "just a function" hosts) - a key written to
    # disk on one invocation is gone by the next, silently invalidating
    # every signed-out-then-back-in session. SESSION_SECRET_KEY (set once in
    # the platform's environment variables) is the production path; the
    # local file remains for local dev, where persisting it there is exactly
    # the convenience it was built for - one real key across restarts with
    # nothing to configure.
    env_key = os.environ.get("SESSION_SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if _SECRET_KEY_PATH.exists():
        return _SECRET_KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    _SECRET_KEY_PATH.write_bytes(key)
    return key


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations_str, salt, digest = stored_hash.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations_str)
    ).hex()
    return secrets.compare_digest(check, digest)


def create_session_token(user_id: int) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{user_id}:{expires_at}"
    signature = hmac.new(_get_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_session_token(token: str) -> int | None:
    # A cookie value that isn't a token this app issued - garbage, truncated,
    # left over from a different auth scheme, or hand-edited in devtools -
    # must never crash the request; it just means "not signed in". Catching
    # broadly here (rather than only ValueError/UnicodeDecodeError) matters:
    # a non-ASCII cookie value raises UnicodeEncodeError at the .encode()
    # step below, which previously escaped this except clause entirely and
    # crashed the request during dependency resolution, before any of
    # main.py's own error handling ran - producing a raw plain-text 500 from
    # Starlette's default handler instead of a JSON error the frontend could
    # parse.
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        user_id_str, expires_at_str, signature = raw.split(":")
        payload = f"{user_id_str}:{expires_at_str}"
        expected_signature = hmac.new(
            _get_secret_key(), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        if int(expires_at_str) < time.time():
            return None
        return int(user_id_str)
    except Exception:
        return None
