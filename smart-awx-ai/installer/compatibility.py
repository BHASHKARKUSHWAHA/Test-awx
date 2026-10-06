"""Compatibility gate (spec section 2: DETECT -> COMPATIBILITY CHECK).

The point of this module is to fail *before* anything is installed when the
machine cannot host the stack, and to say plainly which requirement is missing
instead of letting K3s or AWX die halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .common import C, CompatibilityError, Log, run
from .hardware import Hardware

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _version_tuple(value: str) -> tuple:
    parts = []
    for chunk in str(value).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def evaluate(hardware: Hardware, config: Dict[str, Any], force: bool = False) -> List[Check]:
    checks: List[Check] = []
    supported_os = config.get("supported_os", {})
    hard = config.get("requirements", {}).get("hard", {})
    recommended = config.get("requirements", {}).get("recommended", {})

    # --- operating system -------------------------------------------------
    os_id = (hardware.os.get("id") or "").lower()
    os_version = hardware.os.get("version") or ""
    spec = supported_os.get(os_id)
    if not spec:
        checks.append(Check("Operating system", FAIL, "%s is not a supported distribution (supported: %s)" % (os_id or "unknown", ", ".join(sorted(supported_os)))))
    elif os_version in (spec.get("tested") or []):
        checks.append(Check("Operating system", PASS, "%s %s (tested target)" % (os_id, os_version)))
    elif os_version in (spec.get("supported") or []):
        checks.append(Check("Operating system", PASS, "%s %s (supported)" % (os_id, os_version)))
    elif _version_tuple(os_version) >= _version_tuple(spec.get("minimum", "0")):
        checks.append(Check("Operating system", WARN, "%s %s is newer/older than the tested set %s; continuing" % (os_id, os_version, spec.get("supported"))))
    else:
        checks.append(Check("Operating system", FAIL, "%s %s is below the minimum %s" % (os_id, os_version, spec.get("minimum"))))

    # --- architecture -----------------------------------------------------
    arch = hardware.cpu.get("architecture")
    if arch in (config.get("supported_arch") or []):
        checks.append(Check("Architecture", PASS, str(arch)))
    else:
        checks.append(Check("Architecture", FAIL, "%s is not supported (need one of %s)" % (arch, config.get("supported_arch"))))

    # --- CPU --------------------------------------------------------------
    cores = hardware.cpu.get("logical_cores") or 0
    if cores >= recommended.get("cpu_cores", 4):
        checks.append(Check("CPU cores", PASS, "%d cores" % cores))
    elif cores >= hard.get("cpu_cores", 2):
        checks.append(Check("CPU cores", WARN, "%d cores - AWX job runs will be slow (recommended %d)" % (cores, recommended.get("cpu_cores", 4))))
    else:
        checks.append(Check("CPU cores", FAIL, "%d cores, minimum is %d" % (cores, hard.get("cpu_cores", 2))))

    # --- memory -----------------------------------------------------------
    ram = hardware.memory.get("total_mb") or 0
    if ram >= recommended.get("ram_mb", 8000):
        checks.append(Check("RAM", PASS, "%d MB" % ram))
    elif ram >= hard.get("ram_mb", 3500):
        checks.append(Check("RAM", WARN, "%d MB - below the %d MB recommendation; profile %s will be used and AWX may be slow" % (ram, recommended.get("ram_mb", 8000), hardware.profile)))
    else:
        checks.append(Check("RAM", FAIL, "%d MB is under the %d MB hard minimum for K3s + AWX + a local model" % (ram, hard.get("ram_mb", 3500))))

    # --- disk -------------------------------------------------------------
    free = hardware.disk.get("free_gb") or 0
    if free >= recommended.get("disk_free_gb", 50):
        checks.append(Check("Free disk", PASS, "%.1f GB free" % free))
    elif free >= hard.get("disk_free_gb", 20):
        checks.append(Check("Free disk", WARN, "%.1f GB free - AWX images plus a local model need headroom (recommended %d GB)" % (free, recommended.get("disk_free_gb", 50))))
    else:
        checks.append(Check("Free disk", FAIL, "%.1f GB free is under the %d GB hard minimum" % (free, hard.get("disk_free_gb", 20))))

    # --- init system / cgroups -------------------------------------------
    if hardware.kernel.get("systemd"):
        checks.append(Check("systemd", PASS, "available"))
    else:
        checks.append(Check("systemd", FAIL, "no systemd detected; K3s, Ollama and the agent service all require it"))

    if hardware.kernel.get("cgroup_v2"):
        checks.append(Check("cgroups", PASS, "cgroup v2"))
    else:
        checks.append(Check("cgroups", WARN, "cgroup v2 not detected; K3s will fall back to v1"))

    # --- swap -------------------------------------------------------------
    swap = hardware.memory.get("swap_total_mb") or 0
    if ram < 9000 and swap == 0:
        checks.append(Check("Swap", WARN, "no swap on a %d MB machine; a model load spike can OOM-kill AWX pods" % ram))
    else:
        checks.append(Check("Swap", PASS, "%d MB" % swap))

    # --- connectivity -----------------------------------------------------
    net = run(["getent", "hosts", "get.k3s.io"], mutating=False, timeout=20)
    if net.ok:
        checks.append(Check("DNS / internet", PASS, "name resolution works"))
    else:
        checks.append(Check("DNS / internet", FAIL, "cannot resolve get.k3s.io; K3s, AWX images and the AI model all need an initial download"))

    # --- port availability ------------------------------------------------
    busy = _busy_ports(config)
    if busy:
        checks.append(Check("Ports", WARN, "already in use: %s (install will reuse them only if they belong to this stack)" % ", ".join(busy)))
    else:
        checks.append(Check("Ports", PASS, "6443/30080/30081/11434 free"))

    # --- GPU (informational) ---------------------------------------------
    if hardware.gpu.get("present"):
        checks.append(Check("GPU", PASS, "%s - Ollama will use it when the driver allows" % (hardware.gpu.get("name") or hardware.gpu.get("vendor"))))
    else:
        checks.append(Check("GPU", WARN, "no GPU detected - the local model will run on CPU"))

    if force:
        checks = [Check(c.name, WARN if c.status == FAIL else c.status, c.detail + (" [forced]" if c.status == FAIL else "")) for c in checks]
    return checks


def _busy_ports(config: Dict[str, Any]) -> List[str]:
    ports = ["6443", "30080", "30081", "11434"]
    res = run(["ss", "-ltnH"], mutating=False, timeout=15)
    if not res.ok:
        return []
    busy = []
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        port = local.rsplit(":", 1)[-1]
        if port in ports and port not in busy:
            busy.append(port)
    return busy


def enforce(checks: List[Check]) -> None:
    """Print the matrix and abort when a hard requirement failed."""
    width = max(len(c.name) for c in checks)
    for check in checks:
        colour = {PASS: C.GREEN, WARN: C.YELLOW, FAIL: C.RED}[check.status]
        print("  %-*s : %s  %s" % (width, check.name, C.p(colour, "%-4s" % check.status), check.detail))
    failures = [c for c in checks if c.status == FAIL]
    if failures:
        raise CompatibilityError(
            "compatibility check failed:\n"
            + "\n".join("  - %s: %s" % (c.name, c.detail) for c in failures)
            + "\n\nFix the items above, or re-run with --force to override at your own risk."
        )
    warnings = [c for c in checks if c.status == WARN]
    if warnings:
        Log.warn("%d warning(s) - continuing" % len(warnings))
