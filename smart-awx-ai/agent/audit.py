"""Audit log and secret redaction (spec sections 37 and 51).

Every tool execution - allowed, denied or approved - appends one JSON line to
``/var/log/smart-awx-ai/audit.log``.  Values that look like credentials are
masked before anything is written or returned to the UI.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

AUDIT_LOG = os.environ.get("AGENT_AUDIT_LOG", "/var/log/smart-awx-ai/audit.log")
MASK = "********"
_guard = threading.Lock()

# Ordered: the first pattern that matches a span wins.
_PATTERNS = [
    # key: value / key=value forms
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|secret_key|token|access_token|api[_-]?key|apikey|auth|authorization|bearer|credential)\b(\s*[:=]\s*|\"\s*:\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\)]+)"), r"\1\2" + MASK),
    # command line flags
    (re.compile(r"(?i)(--(?:password|token|api-key|secret)[=\s]+)(\S+)"), r"\1" + MASK),
    # private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "-----BEGIN PRIVATE KEY----- " + MASK + " -----END PRIVATE KEY-----"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), MASK),
    # AWX / generic long hex or base64 tokens following "Token "
    (re.compile(r"(?i)\b(Token|Basic|Bearer)\s+[A-Za-z0-9+/=_\-]{16,}"), r"\1 " + MASK),
    # ssh private key blobs in one line
    (re.compile(r"\bssh-(rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{40,}"), r"ssh-\1 " + MASK),
]

SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "credentials", "private_key",
    "ssh_key", "awx_password", "awx_token", "session", "cookie",
}


def redact_text(text: Optional[str]) -> str:
    if not text:
        return "" if text is None else text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    # Environment values for known-sensitive names, e.g. AWX_PASSWORD=hunter2
    out = re.sub(r"(?i)\b([A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|APIKEY|API_KEY)[A-Z0-9_]*)=(\S+)", r"\1=" + MASK, out)
    return out


def redact(value: Any) -> Any:
    """Recursively redact strings, dict values under sensitive keys, and lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                cleaned[key] = MASK
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def write(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append one redacted audit record and return what was written."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: redact(v) for k, v in entry.items()},
    }
    line = json.dumps(record, default=str, sort_keys=True)
    with _guard:
        try:
            os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
            with open(AUDIT_LOG, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            try:
                os.chmod(AUDIT_LOG, 0o640)
            except OSError:
                pass
        except OSError:
            # Never let an unwritable audit log break the agent; the record is
            # still returned to the caller and shown in the UI trace.
            pass
    return record


def tail(limit: int = 50, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-2000:]
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if conversation_id and row.get("conversation_id") != conversation_id:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return list(reversed(rows))
