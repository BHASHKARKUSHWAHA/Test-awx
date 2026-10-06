"""Permission engine (spec sections 22, 23, 50).

Two rules make this safe rather than decorative:

1. The effective level of a call is the **stricter** of the level declared on
   the tool and the level in ``policies/permissions.yaml``.  The model cannot
   lower it, because the model never supplies it.
2. Approval for a write action is a server-side token with a TTL, bound to one
   exact (tool, arguments) pair.  A HIGH_RISK token additionally requires the
   user to type the confirmation word.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from tools.registry import Level, Tool, get as get_tool

POLICY_PATH = os.environ.get("AGENT_POLICY_PATH", "/opt/smart-awx-ai/policies/permissions.yaml")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": 0.0, "document": None}

_DEFAULT_POLICY: Dict[str, Any] = {
    "policy": {
        "auto_safe_write": True,
        "auto_medium_risk": False,
        "auto_high_risk": False,
        "high_risk_confirmation_word": "CONFIRM",
        "approval_ttl_seconds": 300,
        "max_tool_calls_per_request": 12,
        "default_tool_timeout": 30,
    },
    "overrides": {},
    "safe_write_services": [],
    "critical_services": ["k3s", "ssh", "sshd", "systemd-journald", "dbus"],
    "protected_namespaces": ["kube-system", "kube-public", "kube-node-lease", "default", "awx"],
    "installable_packages": [],
    "readable_log_paths": [],
    "agent_factory": {"max_agents": 8, "allowed_tool_categories": [], "max_permission_level": "safe_write"},
}


def _load_yaml(path: str) -> Any:
    try:
        import yaml  # type: ignore

        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except ImportError:
        import sys

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from installer import minyaml

        return minyaml.load(path)


def policy_document(reload: bool = False) -> Dict[str, Any]:
    """Return the policy document, reloading it when the file changes on disk."""
    with _lock:
        try:
            mtime = os.path.getmtime(POLICY_PATH)
        except OSError:
            if _cache["document"] is None:
                _cache["document"] = dict(_DEFAULT_POLICY)
            return _cache["document"]
        if reload or _cache["document"] is None or mtime != _cache["mtime"]:
            try:
                document = _load_yaml(POLICY_PATH) or {}
            except Exception:  # noqa: BLE001 - a broken policy must not open the gates
                document = dict(_DEFAULT_POLICY)
            merged = dict(_DEFAULT_POLICY)
            merged.update(document)
            merged["policy"] = {**_DEFAULT_POLICY["policy"], **(document.get("policy") or {})}
            _cache["document"] = merged
            _cache["mtime"] = mtime
        return _cache["document"]


def settings() -> Dict[str, Any]:
    return policy_document().get("policy", {})


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------
@dataclass
class Decision:
    tool: str
    level: Level
    allowed: bool
    requires_approval: bool
    reason: str = ""
    risk_note: str = ""
    confirmation_word: Optional[str] = None
    escalated_from: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "level": self.level.value,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "risk_note": self.risk_note,
            "confirmation_word": self.confirmation_word,
            "escalated_from": self.escalated_from,
        }


def _contextual_level(definition: Tool, args: Dict[str, Any], document: Dict[str, Any]) -> tuple[Level, str]:
    """Raise the level based on *what* the call targets, not just which tool."""
    name = definition.name
    if name in ("restart_service", "stop_service"):
        unit = str(args.get("name", "")).replace(".service", "")
        if unit in (document.get("critical_services") or []):
            return Level.HIGH_RISK, "%s is a critical service" % unit
        if name == "restart_service" and unit not in (document.get("safe_write_services") or []):
            return Level.MEDIUM_RISK, "%s is not in safe_write_services" % unit
    if name == "delete_kubernetes_namespace":
        namespace = str(args.get("namespace", ""))
        if namespace in (document.get("protected_namespaces") or []):
            return Level.HIGH_RISK, "%s is a protected namespace and will be refused by the tool" % namespace
    if name == "install_package":
        allowed = document.get("installable_packages") or []
        if allowed and args.get("name") not in allowed:
            return Level.HIGH_RISK, "%s is not in installable_packages" % args.get("name")
    if name == "scale_deployment" and int(args.get("replicas", 1) or 0) == 0:
        return Level.HIGH_RISK, "scaling to zero takes the workload offline"
    return definition.level, ""


def evaluate(tool_name: str, args: Optional[Dict[str, Any]] = None) -> Decision:
    """Decide whether ``tool_name`` may run now, needs approval, or is blocked."""
    args = dict(args or {})
    document = policy_document()
    config = document.get("policy", {})
    try:
        definition = get_tool(tool_name)
    except Exception:
        return Decision(tool=tool_name, level=Level.HIGH_RISK, allowed=False, requires_approval=False, reason="unknown tool")

    declared = definition.level
    override = (document.get("overrides") or {}).get(tool_name)
    levels = [declared]
    if override:
        try:
            levels.append(Level.coerce(override))
        except ValueError:
            pass
    contextual, context_reason = _contextual_level(definition, args, document)
    levels.append(contextual)

    effective = max(levels, key=lambda level: level.rank)
    escalated_from = declared.value if effective is not declared and effective.rank > declared.rank else None

    if effective is Level.READ_ONLY:
        return Decision(tool_name, effective, True, False, reason="read-only tool", risk_note=definition.risk_note)
    if effective is Level.SAFE_WRITE:
        auto = bool(config.get("auto_safe_write", True))
        return Decision(
            tool_name,
            effective,
            True,
            not auto,
            reason="safe write (policy auto_safe_write=%s)" % auto,
            risk_note=definition.risk_note,
            escalated_from=escalated_from,
        )
    if effective is Level.MEDIUM_RISK:
        auto = bool(config.get("auto_medium_risk", False))
        return Decision(
            tool_name,
            effective,
            True,
            not auto,
            reason=context_reason or "medium risk: changes configuration or installs software",
            risk_note=definition.risk_note,
            escalated_from=escalated_from,
        )
    return Decision(
        tool_name,
        Level.HIGH_RISK,
        True,
        True,
        reason=context_reason or "high risk: destructive and not reversible",
        risk_note=definition.risk_note,
        confirmation_word=str(config.get("high_risk_confirmation_word", "CONFIRM")),
        escalated_from=escalated_from,
    )


# --------------------------------------------------------------------------
# Approvals (spec section 23)
# --------------------------------------------------------------------------
@dataclass
class Approval:
    token: str
    tool: str
    args: Dict[str, Any]
    level: Level
    conversation_id: str
    created_at: float
    expires_at: float
    confirmation_word: Optional[str]
    risk_note: str = ""
    reason: str = ""
    used: bool = False

    def fingerprint(self) -> str:
        payload = json.dumps({"tool": self.tool, "args": self.args}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "tool": self.tool,
            "args": self.args,
            "level": self.level.value,
            "risk": self.level.value.replace("_", " ").upper(),
            "risk_note": self.risk_note,
            "reason": self.reason,
            "confirmation_word": self.confirmation_word,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
            "fingerprint": self.fingerprint(),
        }


class ApprovalStore:
    """In-memory, TTL bounded approval tokens.  Never persisted to disk."""

    def __init__(self) -> None:
        self._items: Dict[str, Approval] = {}
        self._guard = threading.Lock()

    def create(self, decision: Decision, args: Dict[str, Any], conversation_id: str) -> Approval:
        ttl = int(settings().get("approval_ttl_seconds", 300))
        now = time.time()
        approval = Approval(
            token=secrets.token_urlsafe(24),
            tool=decision.tool,
            args=dict(args),
            level=decision.level,
            conversation_id=conversation_id,
            created_at=now,
            expires_at=now + ttl,
            confirmation_word=decision.confirmation_word,
            risk_note=decision.risk_note,
            reason=decision.reason,
        )
        with self._guard:
            self._purge()
            self._items[approval.token] = approval
        return approval

    def get(self, token: str) -> Optional[Approval]:
        with self._guard:
            self._purge()
            return self._items.get(token)

    def consume(self, token: str, typed_confirmation: Optional[str] = None) -> Approval:
        with self._guard:
            self._purge()
            approval = self._items.get(token)
            if approval is None:
                raise PermissionError("this approval has expired or was already used - ask again")
            if approval.used:
                raise PermissionError("this approval was already used")
            if approval.confirmation_word:
                if (typed_confirmation or "").strip() != approval.confirmation_word:
                    raise PermissionError(
                        "type %s exactly to confirm this %s action" % (approval.confirmation_word, approval.level.value)
                    )
            approval.used = True
            del self._items[token]
            return approval

    def pending(self, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._guard:
            self._purge()
            return [
                a.to_dict()
                for a in self._items.values()
                if conversation_id is None or a.conversation_id == conversation_id
            ]

    def _purge(self) -> None:
        now = time.time()
        for token in [t for t, a in self._items.items() if a.expires_at < now]:
            del self._items[token]


approvals = ApprovalStore()


def agent_factory_limits() -> Dict[str, Any]:
    return policy_document().get("agent_factory", {})
