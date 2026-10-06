"""Tool registry, schema validation and guarded command execution.

Design rules enforced here:

* a tool is the only way the agent can touch the machine;
* every tool declares name, description, input schema, permission level and
  timeout (spec section 50);
* commands are executed as argv lists, never through a shell, and only
  binaries on the allow-list may be executed at all;
* every execution returns structured data plus the raw output it came from, so
  the answer the user sees can always be traced back to a real command.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence


class Level(str, Enum):
    READ_ONLY = "read_only"
    SAFE_WRITE = "safe_write"
    MEDIUM_RISK = "medium_risk"
    HIGH_RISK = "high_risk"

    @property
    def rank(self) -> int:
        return {"read_only": 0, "safe_write": 1, "medium_risk": 2, "high_risk": 3}[self.value]

    @classmethod
    def coerce(cls, value: Any) -> "Level":
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().lower())


class ToolError(RuntimeError):
    """Raised for a predictable tool failure (bad input, blocked target)."""


# --------------------------------------------------------------------------
# Command execution guard
# --------------------------------------------------------------------------
# The agent may only ever execute these binaries.  Adding to this list is a
# deliberate act; the LLM cannot extend it at runtime.
ALLOWED_BINARIES = {
    # read-only system inspection
    "uname", "uptime", "cat", "free", "df", "du", "lscpu", "lsblk", "ps", "top",
    "who", "id", "hostname", "date", "stat", "find", "journalctl", "dmesg",
    "systemctl", "service", "getent", "tail", "head", "nproc", "vmstat",
    "iostat", "sar", "lsof", "mount", "swapon", "timedatectl", "lsmod",
    # network
    "ip", "ss", "ping", "dig", "nslookup", "host", "traceroute", "curl", "nc",
    "ufw", "iptables", "arp", "route", "netstat",
    # kubernetes / awx
    "kubectl", "k3s", "crictl",
    # ai runtime
    "ollama",
    # package management (medium risk tools only)
    "apt-get", "dpkg-query", "dpkg", "apt-cache",
}

KUBECONFIG = os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")


@dataclass
class Exec:
    """Result of one guarded command execution."""

    cmd: List[str]
    code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def display(self) -> str:
        return " ".join(shlex.quote(part) for part in self.cmd)


def sh(cmd: Sequence[str], timeout: int = 20, env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> Exec:
    """Execute an allow-listed binary with argv (never a shell string)."""
    cmd = [str(part) for part in cmd]
    if not cmd:
        raise ToolError("empty command")
    binary = os.path.basename(cmd[0])
    if binary not in ALLOWED_BINARIES:
        raise ToolError("binary %r is not on the agent allow-list" % binary)
    if shutil.which(cmd[0]) is None and not os.path.exists(cmd[0]):
        return Exec(cmd, 127, "", "%s is not installed on this machine" % binary, 0)

    full_env = dict(os.environ)
    full_env.setdefault("KUBECONFIG", KUBECONFIG)
    full_env["LC_ALL"] = "C"
    if env:
        full_env.update(env)

    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=full_env, input=input_text)
        return Exec(cmd, proc.returncode, proc.stdout or "", proc.stderr or "", int((time.time() - started) * 1000))
    except subprocess.TimeoutExpired:
        return Exec(cmd, 124, "", "timed out after %ss" % timeout, int((time.time() - started) * 1000), timed_out=True)
    except FileNotFoundError:
        return Exec(cmd, 127, "", "%s not found" % binary, int((time.time() - started) * 1000))
    except PermissionError as exc:
        return Exec(cmd, 126, "", str(exc), int((time.time() - started) * 1000))


# --------------------------------------------------------------------------
# Tool definition and registry
# --------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    category: str
    level: Level
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    timeout: int = 30
    # One line shown in the approval prompt for write-level tools.
    risk_note: str = ""
    # When true the planner may call this tool on its own initiative.
    autonomous: bool = True

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "permission": self.level.value,
            "timeout": self.timeout,
            "parameters": self.parameters,
            "risk_note": self.risk_note,
        }


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    error: str = ""
    # Raw evidence: the commands that produced ``data``.  This is what makes
    # "SYSTEM DATA = TOOL OUTPUT" auditable rather than a slogan.
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "evidence": self.evidence,
            "duration_ms": self.duration_ms,
            "args": self.args,
        }


_REGISTRY: Dict[str, Tool] = {}


def tool(
    name: str,
    description: str,
    category: str,
    level: Level = Level.READ_ONLY,
    parameters: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    risk_note: str = "",
    autonomous: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``func`` as a tool.  Handlers return ``(data, evidence)``."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise RuntimeError("duplicate tool name: %s" % name)
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            category=category,
            level=Level.coerce(level),
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            handler=func,
            timeout=timeout,
            risk_note=risk_note,
            autonomous=autonomous,
        )
        func.tool_name = name  # type: ignore[attr-defined]
        return func

    return decorator


def get(name: str) -> Tool:
    if name not in _REGISTRY:
        raise ToolError("unknown tool: %s" % name)
    return _REGISTRY[name]


def all_tools() -> Dict[str, Tool]:
    return dict(_REGISTRY)


def by_category(category: str) -> List[Tool]:
    return [t for t in _REGISTRY.values() if t.category == category]


def catalog() -> List[Dict[str, Any]]:
    return [t.schema() for t in sorted(_REGISTRY.values(), key=lambda t: (t.category, t.name))]


def reset_for_tests() -> None:
    _REGISTRY.clear()


# --------------------------------------------------------------------------
# Input validation (small, dependency free JSON-Schema subset)
# --------------------------------------------------------------------------
_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(spec: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce ``args`` against the tool's parameter schema."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    properties = spec.get("properties") or {}
    required = spec.get("required") or []
    unknown = set(args) - set(properties)
    if unknown:
        raise ToolError("unknown argument(s): %s" % ", ".join(sorted(unknown)))
    for key in required:
        if key not in args or args[key] in (None, ""):
            raise ToolError("missing required argument: %s" % key)

    cleaned: Dict[str, Any] = {}
    for key, value in args.items():
        rules = properties[key]
        expected = rules.get("type", "string")
        python_type = _TYPES.get(expected, str)
        if expected == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if expected == "boolean" and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        if value is not None and not isinstance(value, python_type):
            raise ToolError("argument %r must be %s" % (key, expected))
        if "enum" in rules and value not in rules["enum"]:
            raise ToolError("argument %r must be one of %s" % (key, rules["enum"]))
        if isinstance(value, str):
            if "pattern" in rules and not re.fullmatch(rules["pattern"], value):
                raise ToolError("argument %r has an invalid format" % key)
            if "maxLength" in rules and len(value) > rules["maxLength"]:
                raise ToolError("argument %r is too long" % key)
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in rules and value < rules["minimum"]:
                raise ToolError("argument %r must be >= %s" % (key, rules["minimum"]))
            if "maximum" in rules and value > rules["maximum"]:
                raise ToolError("argument %r must be <= %s" % (key, rules["maximum"]))
        cleaned[key] = value

    for key, rules in properties.items():
        if key not in cleaned and "default" in rules:
            cleaned[key] = rules["default"]
    return cleaned


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def execute(name: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
    """Run a tool after validating its input.  Never raises for tool failure."""
    args = dict(args or {})
    started = time.time()
    try:
        definition = get(name)
        cleaned = validate_args(definition.parameters, args)
    except ToolError as exc:
        return ToolResult(tool=name, ok=False, error=str(exc), args=args, duration_ms=0)

    try:
        outcome = definition.handler(**cleaned)
        if isinstance(outcome, tuple) and len(outcome) == 2:
            data, evidence = outcome
        else:
            data, evidence = outcome, []
        evidence_rows = [_evidence_row(item) for item in (evidence or [])]
        failed = [row for row in evidence_rows if row.get("exit_code") not in (0, None)]
        # A tool that ran but whose every underlying command failed is a
        # failure, not a success with empty data - the agent must never dress
        # up "ss is not installed" as an answer.
        succeeded = bool(not evidence_rows or len(failed) < len(evidence_rows))
        return ToolResult(
            tool=name,
            ok=succeeded,
            data=data,
            evidence=evidence_rows,
            duration_ms=int((time.time() - started) * 1000),
            args=cleaned,
            error="; ".join(row["stderr"] for row in failed if row.get("stderr"))[:500],
        )
    except ToolError as exc:
        return ToolResult(tool=name, ok=False, error=str(exc), args=cleaned, duration_ms=int((time.time() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        return ToolResult(
            tool=name,
            ok=False,
            error="%s: %s" % (type(exc).__name__, exc),
            args=cleaned,
            duration_ms=int((time.time() - started) * 1000),
        )


def _evidence_row(item: Any) -> Dict[str, Any]:
    if isinstance(item, Exec):
        return {
            "command": item.display,
            "exit_code": item.code,
            "stdout": item.stdout[-4000:],
            "stderr": item.stderr[-1000:],
            "duration_ms": item.duration_ms,
        }
    if isinstance(item, dict):
        return item
    return {"command": str(item), "exit_code": None, "stdout": "", "stderr": ""}
