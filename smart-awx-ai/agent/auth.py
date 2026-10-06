"""Authentication for the AI Agent (spec sections 33 and 34).

Three ways in, in order of preference:

1. **AWX session reuse** - the AI proxy serves the agent on the same origin as
   AWX, so the browser sends AWX's ``awx_sessionid`` cookie.  The backend
   verifies it by calling AWX ``/api/v2/me/`` with that cookie.  If AWX says
   who you are, you are that user.
2. **AWX credentials** - username/password posted to ``/api/ai/login`` are
   verified against the AWX API and then exchanged for a signed session cookie.
   The credentials themselves are never stored.
3. **Local break-glass account** - a username and a PBKDF2 hash generated at
   install time, used when AWX itself is down (which is exactly when the
   self-healing agent is needed most).

There is no unauthenticated path to any tool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from tools.awx import AWXClient, AWXError

SESSION_COOKIE = "ai_agent_session"
AWX_COOKIE = "awx_sessionid"
SESSION_TTL = int(os.environ.get("AGENT_SESSION_TTL", "28800"))  # 8 hours
PBKDF2_ROUNDS = 200_000


def _secret() -> bytes:
    value = os.environ.get("AGENT_SESSION_SECRET")
    if not value:
        # Ephemeral secret: sessions do not survive a restart, which is safe
        # (everyone simply logs in again) and avoids a weak static default.
        value = secrets.token_urlsafe(32)
        os.environ["AGENT_SESSION_SECRET"] = value
    return value.encode("utf-8")


@dataclass
class Identity:
    username: str
    source: str  # awx_session | awx_login | local
    is_superuser: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"username": self.username, "source": self.source, "is_superuser": self.is_superuser}


# --------------------------------------------------------------------------
# Password hashing for the local break-glass account
# --------------------------------------------------------------------------
def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Signed session tokens
# --------------------------------------------------------------------------
def issue_token(identity: Identity, ttl: int = SESSION_TTL) -> str:
    payload = {"u": identity.username, "s": identity.source, "a": identity.is_superuser, "exp": int(time.time()) + ttl}
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return "%s.%s" % (body, signature)


def read_token(token: Optional[str]) -> Optional[Identity]:
    if not token or "." not in token:
        return None
    body, _, signature = token.rpartition(".")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return Identity(username=str(payload.get("u")), source=str(payload.get("s", "awx_login")), is_superuser=bool(payload.get("a")))


# --------------------------------------------------------------------------
# Credential verification
# --------------------------------------------------------------------------
_awx_session_cache: Dict[str, Tuple[float, Identity]] = {}
AWX_SESSION_CACHE_TTL = 60


def verify_awx_session_cookie(cookie_value: str) -> Optional[Identity]:
    """Ask AWX who owns this session cookie.  Cached briefly to spare the API."""
    if not cookie_value:
        return None
    now = time.time()
    cached = _awx_session_cache.get(cookie_value)
    if cached and cached[0] > now:
        return cached[1]
    client = AWXClient()
    try:
        status, payload = client.request("GET", "/api/v2/me/", headers={"Cookie": "%s=%s" % (AWX_COOKIE, cookie_value)})
    except AWXError:
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    results = payload.get("results") or []
    if not results:
        return None
    identity = Identity(username=results[0].get("username", "awx-user"), source="awx_session", is_superuser=bool(results[0].get("is_superuser")))
    _awx_session_cache[cookie_value] = (now + AWX_SESSION_CACHE_TTL, identity)
    if len(_awx_session_cache) > 200:
        for key in [k for k, (expiry, _) in _awx_session_cache.items() if expiry < now]:
            _awx_session_cache.pop(key, None)
    return identity


def verify_awx_credentials(username: str, password: str) -> Optional[Identity]:
    client = AWXClient(username=username, password=password, token=None)
    client.token = None
    try:
        payload = client.me()
    except AWXError:
        return None
    results = (payload or {}).get("results") or []
    if not results:
        return None
    return Identity(username=results[0].get("username", username), source="awx_login", is_superuser=bool(results[0].get("is_superuser")))


def verify_local_credentials(username: str, password: str) -> Optional[Identity]:
    expected_user = os.environ.get("AGENT_LOCAL_USER")
    expected_hash = os.environ.get("AGENT_LOCAL_PASSWORD_HASH")
    if not expected_user or not expected_hash:
        return None
    if not hmac.compare_digest(username, expected_user):
        return None
    if not verify_password(password, expected_hash):
        return None
    return Identity(username=username, source="local", is_superuser=True)


def authenticate(username: str, password: str) -> Optional[Identity]:
    """AWX first, local break-glass second."""
    identity = verify_awx_credentials(username, password)
    if identity:
        return identity
    return verify_local_credentials(username, password)
