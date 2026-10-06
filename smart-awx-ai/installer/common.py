"""Shared plumbing for the installer: logging, command execution, state."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_ROOT = "/opt/smart-awx-ai"
CONFIG_DIR = "/etc/smart-awx-ai"
STATE_DIR = "/var/lib/smart-awx-ai"
LOG_DIR = "/var/log/smart-awx-ai"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
INSTALL_LOG = os.path.join(LOG_DIR, "install.log")
AGENT_ENV_FILE = os.path.join(CONFIG_DIR, "agent.env")
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"

AWX_NAMESPACE = "awx"
AGENT_SERVICE = "smart-awx-ai-agent"
AGENT_PORT = 8900

PHASES = [
    ("preflight", "System detection and compatibility"),
    ("dependencies", "Base OS dependencies"),
    ("k3s", "K3s Kubernetes"),
    ("awx_operator", "AWX Operator"),
    ("awx", "AWX instance"),
    ("ai_runtime", "AI runtime (Ollama) and local model"),
    ("ai_agent", "AI Agent backend service"),
    ("ai_ui", "AI Agent UI and AWX dashboard integration"),
    ("health", "Full stack health check"),
]


# --------------------------------------------------------------------------
# Secret redaction - used by every log path (spec section 37 / 51)
# --------------------------------------------------------------------------
_REDACT_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|bearer|authorization)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"),
    re.compile(r"(?i)(--password[= ])(\S+)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"),
    # Environment-variable style: AWX_PASSWORD=..., AGENT_SESSION_SECRET=...
    re.compile(r"\b([A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|APIKEY|API_KEY|PASSWD)[A-Z0-9_]*)=(\S+)"),
]
MASK = "********"


def redact(text: str) -> str:
    """Replace anything that looks like a credential with ``********``."""
    if not text:
        return text
    out = text
    out = _REDACT_PATTERNS[0].sub(lambda m: "%s=%s" % (m.group(1), MASK), out)
    out = _REDACT_PATTERNS[1].sub(lambda m: "%s%s" % (m.group(1), MASK), out)
    out = _REDACT_PATTERNS[2].sub("-----BEGIN PRIVATE KEY----- %s -----END PRIVATE KEY-----" % MASK, out)
    out = _REDACT_PATTERNS[3].sub(MASK, out)
    out = _REDACT_PATTERNS[4].sub(lambda m: "%s=%s" % (m.group(1), MASK), out)
    return out


# --------------------------------------------------------------------------
# Console / log output
# --------------------------------------------------------------------------
class C:
    """ANSI colours, disabled automatically when stdout is not a terminal."""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"

    @classmethod
    def p(cls, color: str, text: str) -> str:
        if not cls.enabled:
            return text
        return "%s%s%s" % (color, text, cls.RESET)


class Log:
    verbose = False
    _fh = None

    @classmethod
    def open(cls, path: str = INSTALL_LOG) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cls._fh = open(path, "a", encoding="utf-8")
        except OSError:
            cls._fh = None  # keep running with console-only logging

    @classmethod
    def _write(cls, level: str, message: str) -> None:
        line = "%s [%s] %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), level, redact(message))
        if cls._fh:
            cls._fh.write(line + "\n")
            cls._fh.flush()

    @classmethod
    def info(cls, message: str) -> None:
        cls._write("INFO", message)
        print(redact(message))

    @classmethod
    def ok(cls, message: str) -> None:
        cls._write("OK", message)
        print("%s %s" % (C.p(C.GREEN, "[ OK ]"), redact(message)))

    @classmethod
    def warn(cls, message: str) -> None:
        cls._write("WARN", message)
        print("%s %s" % (C.p(C.YELLOW, "[WARN]"), redact(message)))

    @classmethod
    def error(cls, message: str) -> None:
        cls._write("ERROR", message)
        print("%s %s" % (C.p(C.RED, "[FAIL]"), redact(message)), file=sys.stderr)

    @classmethod
    def step(cls, message: str) -> None:
        cls._write("STEP", message)
        print("%s %s" % (C.p(C.CYAN, "  ->"), redact(message)))

    @classmethod
    def debug(cls, message: str) -> None:
        cls._write("DEBUG", message)
        if cls.verbose:
            print(C.p(C.DIM, "       " + redact(message)))

    @classmethod
    def banner(cls, title: str) -> None:
        bar = "=" * 72
        cls._write("PHASE", title)
        print("\n" + C.p(C.BOLD, bar))
        print(C.p(C.BOLD, title))
        print(C.p(C.BOLD, bar))


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------
DRY_RUN = False


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    cmd: List[str]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    def tail(self, lines: int = 20) -> str:
        blob = (self.stdout + "\n" + self.stderr).strip()
        return "\n".join(blob.splitlines()[-lines:])


def run(
    cmd: Sequence[str],
    timeout: int = 300,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
    check: bool = False,
    mutating: bool = True,
    cwd: Optional[str] = None,
) -> Result:
    """Run ``cmd`` (never through a shell) and return a :class:`Result`.

    ``mutating=True`` commands are skipped in --dry-run mode; read-only probes
    pass ``mutating=False`` so a dry run still reports the truth.
    """
    cmd = [str(part) for part in cmd]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)

    if DRY_RUN and mutating:
        Log.debug("DRY-RUN skip: %s" % " ".join(cmd))
        return Result(0, "[dry-run]", "", list(cmd))

    Log.debug("exec: %s" % " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            input=input_text,
            cwd=cwd,
        )
        result = Result(proc.returncode, proc.stdout or "", proc.stderr or "", list(cmd))
    except subprocess.TimeoutExpired:
        result = Result(124, "", "timed out after %ss" % timeout, list(cmd), timed_out=True)
    except FileNotFoundError as exc:
        result = Result(127, "", str(exc), list(cmd))
    except PermissionError as exc:
        result = Result(126, "", str(exc), list(cmd))

    if not result.ok:
        Log.debug("exit=%d stderr=%s" % (result.code, result.stderr.strip()[:500]))
    if check and not result.ok:
        raise InstallError("command failed: %s\n%s" % (" ".join(cmd), result.tail()))
    return result


def which(binary: str) -> Optional[str]:
    return shutil.which(binary)


class InstallError(RuntimeError):
    """Raised by a phase when it cannot complete."""


class CompatibilityError(InstallError):
    """Raised when the machine does not meet hard requirements."""


# --------------------------------------------------------------------------
# Retry with bounded attempts (spec section 3: max 3 retries, no infinite loop)
# --------------------------------------------------------------------------
def retry(
    action: Callable[[int], Any],
    attempts: int = 3,
    backoff: Sequence[int] = (5, 15, 30),
    on_failure: Optional[Callable[[int, Exception], None]] = None,
    label: str = "operation",
) -> Any:
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return action(attempt)
        except CompatibilityError:
            # A machine that fails a hard requirement will keep failing it;
            # retrying would only waste the operator's time.
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised below after retries
            last = exc
            Log.warn("%s failed (attempt %d/%d): %s" % (label, attempt, attempts, exc))
            if on_failure:
                try:
                    on_failure(attempt, exc)
                except Exception as hook_exc:  # noqa: BLE001
                    Log.warn("recovery hook error: %s" % hook_exc)
            if attempt < attempts:
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                Log.info("retrying %s in %ds ..." % (label, delay))
                time.sleep(delay)
    raise InstallError("%s failed after %d attempts: %s" % (label, attempts, last))


def wait_for(
    predicate: Callable[[], bool],
    timeout: int,
    interval: int = 5,
    label: str = "condition",
    progress: Optional[Callable[[int], str]] = None,
) -> bool:
    """Poll ``predicate`` until true or ``timeout`` seconds elapse."""
    if DRY_RUN:
        Log.debug("DRY-RUN: not waiting for %s" % label)
        return True
    deadline = time.time() + timeout
    last_note = 0.0
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception as exc:  # noqa: BLE001
            Log.debug("wait_for(%s) probe error: %s" % (label, exc))
        now = time.time()
        if progress and now - last_note > 30:
            last_note = now
            note = progress(int(deadline - now))
            if note:
                Log.step(note)
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------
# Installer state (spec section 46: resume after failure)
# --------------------------------------------------------------------------
@dataclass
class PhaseState:
    status: str = "pending"  # pending | running | complete | failed | skipped
    attempts: int = 0
    updated_at: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "updated_at": self.updated_at,
            "detail": self.detail,
        }


@dataclass
class State:
    path: str = STATE_FILE
    phases: Dict[str, PhaseState] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = STATE_FILE) -> "State":
        state = cls(path=path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    blob = json.load(handle)
                for name, raw in (blob.get("phases") or {}).items():
                    state.phases[name] = PhaseState(**raw)
                state.facts = blob.get("facts") or {}
            except (OSError, ValueError, TypeError) as exc:
                Log.warn("could not read state file (%s); starting fresh" % exc)
        for name, _desc in PHASES:
            state.phases.setdefault(name, PhaseState())
        return state

    def save(self) -> None:
        if DRY_RUN:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "phases": {k: v.to_dict() for k, v in self.phases.items()},
                        "facts": self.facts,
                    },
                    handle,
                    indent=2,
                    sort_keys=True,
                )
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            Log.warn("could not persist state: %s" % exc)

    def mark(self, phase: str, status: str, detail: str = "") -> None:
        entry = self.phases.setdefault(phase, PhaseState())
        entry.status = status
        entry.detail = detail
        entry.updated_at = datetime.now(timezone.utc).isoformat()
        if status == "running":
            entry.attempts += 1
        self.save()

    def is_complete(self, phase: str) -> bool:
        return self.phases.get(phase, PhaseState()).status == "complete"

    def set_fact(self, key: str, value: Any) -> None:
        self.facts[key] = value
        self.save()


# --------------------------------------------------------------------------
# Misc helpers
# --------------------------------------------------------------------------
def require_root() -> None:
    if os.geteuid() != 0:
        raise InstallError("this installer must run as root: sudo python3 install.py")


def primary_ip() -> str:
    """Best-effort primary IPv4 of this host (no external traffic is sent)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("198.51.100.1", 53))  # TEST-NET-2, never routed
        addr = sock.getsockname()[0]
        sock.close()
        if addr and not addr.startswith("127."):
            return addr
    except OSError:
        pass
    res = run(["hostname", "-I"], mutating=False, timeout=10)
    for token in res.out.split():
        if ":" not in token and not token.startswith("127."):
            return token
    return "127.0.0.1"


def ensure_dirs() -> None:
    for path, mode in ((INSTALL_ROOT, 0o755), (CONFIG_DIR, 0o750), (STATE_DIR, 0o750), (LOG_DIR, 0o750)):
        if DRY_RUN:
            continue
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def write_file(path: str, content: str, mode: int = 0o644) -> None:
    if DRY_RUN:
        Log.debug("DRY-RUN write %s (%d bytes)" % (path, len(content)))
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)
    os.chmod(path, mode)


def human_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f PB" % num


def load_yaml(path: str) -> Any:
    """Load YAML with PyYAML when present, falling back to the mini parser."""
    try:
        import yaml  # type: ignore

        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except ImportError:
        from . import minyaml  # local import keeps module import cheap

        return minyaml.load(path)
