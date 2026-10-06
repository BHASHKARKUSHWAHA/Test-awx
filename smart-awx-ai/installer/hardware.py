"""Hardware / OS detection (spec section 1).

Everything reported here comes from the machine itself - /proc, /sys, and
standard userspace tools.  Nothing is guessed, and every field that could not
be determined is reported as ``None`` rather than being invented.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .common import run, which


# --------------------------------------------------------------------------
# Individual probes
# --------------------------------------------------------------------------
def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def detect_os() -> Dict[str, Any]:
    info: Dict[str, Any] = {"id": None, "name": None, "version": None, "codename": None, "pretty": None}
    for line in _read("/etc/os-release").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"')
        mapping = {
            "ID": "id",
            "NAME": "name",
            "VERSION_ID": "version",
            "VERSION_CODENAME": "codename",
            "PRETTY_NAME": "pretty",
        }
        if key in mapping:
            info[mapping[key]] = value
    return info


def detect_cpu() -> Dict[str, Any]:
    cpuinfo = _read("/proc/cpuinfo")
    model = None
    for line in cpuinfo.splitlines():
        if line.lower().startswith(("model name", "hardware", "cpu model")):
            model = line.split(":", 1)[1].strip()
            break
    physical = len({
        line.split(":", 1)[1].strip()
        for line in cpuinfo.splitlines()
        if line.startswith("core id")
    }) or None
    load1, load5, load15 = os.getloadavg()
    return {
        "model": model,
        "logical_cores": os.cpu_count(),
        "physical_cores": physical,
        "architecture": platform.machine(),
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "load_15m": round(load15, 2),
        "flags_virtualization": any(flag in cpuinfo for flag in (" vmx", " svm")),
    }


def _meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0])  # kB
    return values


def detect_memory() -> Dict[str, Any]:
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_kb = mem.get("SwapTotal", 0)
    return {
        "total_mb": round(total_kb / 1024),
        "available_mb": round(avail_kb / 1024),
        "used_mb": round((total_kb - avail_kb) / 1024),
        "used_percent": round((total_kb - avail_kb) / total_kb * 100, 1) if total_kb else None,
        "swap_total_mb": round(swap_kb / 1024),
        "swap_free_mb": round(mem.get("SwapFree", 0) / 1024),
    }


def detect_disk(path: str = "/") -> Dict[str, Any]:
    usage = shutil.disk_usage(path)
    var_lib = shutil.disk_usage("/var") if os.path.exists("/var") else usage
    return {
        "path": path,
        "total_gb": round(usage.total / 1024 ** 3, 1),
        "free_gb": round(usage.free / 1024 ** 3, 1),
        "used_gb": round(usage.used / 1024 ** 3, 1),
        "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
        "var_free_gb": round(var_lib.free / 1024 ** 3, 1),
    }


def detect_kernel() -> Dict[str, Any]:
    return {
        "release": platform.release(),
        "version": platform.version(),
        "cgroup_v2": os.path.exists("/sys/fs/cgroup/cgroup.controllers"),
        "systemd": os.path.isdir("/run/systemd/system"),
    }


def detect_network() -> Dict[str, Any]:
    interfaces: List[Dict[str, Any]] = []
    res = run(["ip", "-o", "-4", "addr", "show"], mutating=False, timeout=15)
    if res.ok:
        for line in res.out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                interfaces.append({"name": parts[1], "address": parts[3]})
    gateway = None
    route = run(["ip", "route", "show", "default"], mutating=False, timeout=15)
    if route.ok and route.out:
        match = re.search(r"default via (\S+) dev (\S+)", route.out)
        if match:
            gateway = {"via": match.group(1), "dev": match.group(2)}
    dns: List[str] = []
    for line in _read("/etc/resolv.conf").splitlines():
        if line.startswith("nameserver"):
            dns.append(line.split()[1])
    return {
        "hostname": platform.node(),
        "interfaces": [i for i in interfaces if i["name"] != "lo"],
        "default_gateway": gateway,
        "dns_servers": dns,
    }


def detect_firewall() -> Dict[str, Any]:
    info: Dict[str, Any] = {"ufw_installed": bool(which("ufw")), "ufw_active": False, "nftables": bool(which("nft")), "iptables": bool(which("iptables"))}
    if info["ufw_installed"]:
        res = run(["ufw", "status"], mutating=False, timeout=15)
        info["ufw_active"] = res.ok and "Status: active" in res.stdout
    return info


def detect_gpu() -> Dict[str, Any]:
    gpu: Dict[str, Any] = {"present": False, "vendor": None, "name": None, "vram_mb": None, "cuda": False, "rocm": False}
    if which("nvidia-smi"):
        res = run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            mutating=False,
            timeout=20,
        )
        if res.ok and res.out:
            first = res.out.splitlines()[0]
            name, _, vram = first.partition(",")
            gpu.update(present=True, vendor="nvidia", name=name.strip(), cuda=True)
            try:
                gpu["vram_mb"] = int(vram.strip())
            except ValueError:
                gpu["vram_mb"] = None
            return gpu
    if which("rocm-smi"):
        res = run(["rocm-smi", "--showproductname"], mutating=False, timeout=20)
        if res.ok and "GPU" in res.stdout:
            gpu.update(present=True, vendor="amd", rocm=True, name="AMD GPU (rocm-smi)")
            return gpu
    if which("lspci"):
        res = run(["lspci"], mutating=False, timeout=20)
        if res.ok:
            for line in res.out.splitlines():
                if re.search(r"(VGA compatible controller|3D controller)", line):
                    gpu.update(present=True, name=line.split(":", 2)[-1].strip())
                    low = line.lower()
                    gpu["vendor"] = "nvidia" if "nvidia" in low else "amd" if ("amd" in low or "radeon" in low) else "intel" if "intel" in low else "unknown"
                    break
    return gpu


def detect_virtualization() -> Optional[str]:
    if which("systemd-detect-virt"):
        res = run(["systemd-detect-virt"], mutating=False, timeout=10)
        value = res.out or None
        return value if value and value != "none" else ("none" if res.code == 1 else value)
    return None


def detect_container_runtime() -> Dict[str, bool]:
    return {
        "docker": bool(which("docker")),
        "containerd": bool(which("containerd")),
        "k3s": bool(which("k3s")),
        "kubectl": bool(which("kubectl")),
        "ollama": bool(which("ollama")),
    }


# --------------------------------------------------------------------------
# Profile selection (spec section 4)
# --------------------------------------------------------------------------
@dataclass
class Hardware:
    os: Dict[str, Any] = field(default_factory=dict)
    cpu: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    disk: Dict[str, Any] = field(default_factory=dict)
    kernel: Dict[str, Any] = field(default_factory=dict)
    network: Dict[str, Any] = field(default_factory=dict)
    firewall: Dict[str, Any] = field(default_factory=dict)
    gpu: Dict[str, Any] = field(default_factory=dict)
    virtualization: Optional[str] = None
    runtimes: Dict[str, bool] = field(default_factory=dict)
    profile: str = "TEST"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def select_profile(ram_mb: int, profiles: Dict[str, Any]) -> str:
    """Pick the first profile whose ``max_ram_mb`` covers ``ram_mb``.

    Rules are evaluated small-to-large so the matrix stays declarative; a
    ``max_ram_mb`` of ``None`` means "no upper bound" and wins by default.
    """
    bounded = [
        (name, spec.get("max_ram_mb"))
        for name, spec in profiles.items()
        if spec.get("max_ram_mb") is not None
    ]
    bounded.sort(key=lambda item: item[1])
    for name, limit in bounded:
        if ram_mb <= limit:
            return name
    for name, spec in profiles.items():
        if spec.get("max_ram_mb") is None:
            return name
    return bounded[-1][0] if bounded else "TEST"


def detect(config: Dict[str, Any]) -> Hardware:
    """Run every probe and return a populated :class:`Hardware`."""
    hardware = Hardware(
        os=detect_os(),
        cpu=detect_cpu(),
        memory=detect_memory(),
        disk=detect_disk(),
        kernel=detect_kernel(),
        network=detect_network(),
        firewall=detect_firewall(),
        gpu=detect_gpu(),
        virtualization=detect_virtualization(),
        runtimes=detect_container_runtime(),
    )
    hardware.profile = select_profile(hardware.memory["total_mb"], config.get("profiles", {}))
    return hardware


def report(hardware: Hardware) -> str:
    """Human readable detection summary printed at the start of every run."""
    gpu = hardware.gpu
    gpu_line = "none detected"
    if gpu.get("present"):
        gpu_line = gpu.get("name") or gpu.get("vendor") or "present"
        if gpu.get("vram_mb"):
            gpu_line += " (%d MB VRAM)" % gpu["vram_mb"]
    nics = ", ".join("%s=%s" % (i["name"], i["address"]) for i in hardware.network.get("interfaces", [])) or "none"
    rows = [
        ("OS", hardware.os.get("pretty") or "unknown"),
        ("OS ID / version", "%s %s" % (hardware.os.get("id"), hardware.os.get("version"))),
        ("Kernel", hardware.kernel.get("release")),
        ("Architecture", hardware.cpu.get("architecture")),
        ("CPU", hardware.cpu.get("model") or "unknown"),
        ("CPU cores", "%s logical" % hardware.cpu.get("logical_cores")),
        ("Load avg", "%s / %s / %s" % (hardware.cpu.get("load_1m"), hardware.cpu.get("load_5m"), hardware.cpu.get("load_15m"))),
        ("RAM", "%s MB total, %s MB available" % (hardware.memory.get("total_mb"), hardware.memory.get("available_mb"))),
        ("Swap", "%s MB" % hardware.memory.get("swap_total_mb")),
        ("Disk /", "%s GB total, %s GB free" % (hardware.disk.get("total_gb"), hardware.disk.get("free_gb"))),
        ("Network", nics),
        ("Gateway", hardware.network.get("default_gateway") or "none"),
        ("Firewall", "ufw active" if hardware.firewall.get("ufw_active") else "ufw inactive/absent"),
        ("GPU", gpu_line),
        ("Virtualization", hardware.virtualization or "unknown"),
        ("systemd", "yes" if hardware.kernel.get("systemd") else "no"),
        ("cgroup v2", "yes" if hardware.kernel.get("cgroup_v2") else "no"),
        ("Hardware profile", hardware.profile),
    ]
    width = max(len(name) for name, _ in rows)
    return "\n".join("  %-*s : %s" % (width, name, value) for name, value in rows)
