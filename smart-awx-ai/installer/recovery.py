"""Failure handling: ERROR -> COLLECT LOGS -> DIAGNOSE -> SAFE RECOVERY -> RETRY.

Bounded by design (spec section 3): at most three attempts per phase, every
recovery action is reversible, and nothing is ever wiped to "start clean".
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import ai as ai_phase
from . import awx as awx_phase
from . import kubernetes as k8s_phase
from .common import LOG_DIR, Log, redact, run

# Known failure signatures -> (plain diagnosis, what the installer will try).
SIGNATURES: List[Tuple[str, str, str]] = [
    ("no space left on device", "The disk filled up.", "Free space (container images and model blobs are the usual culprits) and re-run."),
    ("Cannot allocate memory", "The machine ran out of RAM.", "Lower the profile sizing in config/compatibility.yaml or add swap, then re-run."),
    ("OOMKilled", "A container was killed for exceeding its memory limit.", "Raise that container's limit in config/compatibility.yaml -> sizing, or use a smaller AI model."),
    ("ImagePullBackOff", "A container image could not be pulled.", "Check outbound access to the registry, then re-run; the installer will retry the pull."),
    ("ErrImagePull", "A container image could not be pulled.", "Check outbound access to the registry and any proxy settings."),
    ("connection refused", "A service that should be listening is not up yet, or crashed.", "The installer restarts the unit and waits again."),
    ("Temporary failure in name resolution", "DNS resolution failed.", "Check /etc/resolv.conf and the default route."),
    ("certificate", "A TLS certificate was rejected.", "Check the system clock and any corporate TLS interception."),
    ("Unable to connect to the server", "The Kubernetes API is not reachable.", "The installer restarts k3s and waits for the node to report Ready."),
    ("CrashLoopBackOff", "A pod keeps crashing after start.", "Logs for the crashing pod are collected; the installer deletes it once so its controller recreates it."),
    ("Permission denied", "A file or socket could not be accessed.", "Confirm the installer is running as root (sudo python3 install.py)."),
    ("manifest unknown", "The requested image or model tag does not exist.", "The installer falls back to the next configured version/model."),
    ("pull model manifest", "The AI model could not be downloaded.", "The installer tries the next model in the fallback chain."),
]


def diagnose(error_text: str) -> Dict[str, str]:
    blob = redact(str(error_text))
    for needle, cause, action in SIGNATURES:
        if needle.lower() in blob.lower():
            return {"matched": needle, "cause": cause, "suggested_action": action}
    return {
        "matched": "",
        "cause": "No known failure signature matched.",
        "suggested_action": "Read the collected diagnostics bundle under %s for the full output." % LOG_DIR,
    }


def collect_logs(phase: str, error_text: str = "") -> Optional[str]:
    """Write one diagnostics bundle per failure and return its path."""
    from .common import DRY_RUN

    if DRY_RUN:
        Log.debug("DRY-RUN: skipping the diagnostics bundle for phase %s" % phase)
        return None
    collectors: Dict[str, Callable[[], str]] = {
        "k3s": k8s_phase.diagnostics,
        "awx_operator": awx_phase.diagnostics,
        "awx": awx_phase.diagnostics,
        "ai_runtime": ai_phase.diagnostics,
        "ai_agent": ai_phase.diagnostics,
        "ai_ui": ai_phase.diagnostics,
    }
    sections = ["### failure in phase: %s" % phase, "### error\n%s" % redact(str(error_text))[:4000]]
    diagnosis = diagnose(error_text)
    sections.append("### diagnosis\ncause: %s\naction: %s" % (diagnosis["cause"], diagnosis["suggested_action"]))

    collector = collectors.get(phase)
    if collector:
        try:
            sections.append(collector())
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            sections.append("### collector error\n%s" % exc)

    for label, cmd in (
        ("free -m", ["free", "-m"]),
        ("df -h", ["df", "-h"]),
        ("uptime", ["uptime"]),
        ("dmesg (last 30)", ["dmesg", "-T", "--level=err,warn"]),
    ):
        res = run(cmd, mutating=False, timeout=30)
        sections.append("### %s\n%s" % (label, res.tail(30)))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(LOG_DIR, "diagnostics-%s-%s.log" % (phase, stamp))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(redact("\n\n".join(sections)))
        os.chmod(path, 0o640)
    except OSError as exc:
        Log.warn("could not write the diagnostics bundle: %s" % exc)
        return None
    return path


RECOVERY: Dict[str, Callable[[int], None]] = {
    "k3s": k8s_phase.recover,
    "awx_operator": awx_phase.recover,
    "awx": awx_phase.recover,
    "ai_runtime": ai_phase.recover,
    "ai_agent": ai_phase.recover,
    "ai_ui": ai_phase.recover,
}


def handle_failure(phase: str) -> Callable[[int, Exception], None]:
    """Build the on_failure hook passed to :func:`common.retry`."""

    def hook(attempt: int, error: Exception) -> None:
        Log.warn("phase %s failed on attempt %d - collecting diagnostics" % (phase, attempt))
        path = collect_logs(phase, str(error))
        diagnosis = diagnose(str(error))
        if diagnosis["matched"]:
            Log.info("Diagnosis: %s" % diagnosis["cause"])
            Log.info("Next: %s" % diagnosis["suggested_action"])
        if path:
            Log.info("Diagnostics bundle: %s" % path)
        recover = RECOVERY.get(phase)
        if recover:
            try:
                recover(attempt)
            except Exception as exc:  # noqa: BLE001
                Log.warn("safe recovery for %s raised: %s" % (phase, exc))

    return hook
