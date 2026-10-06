"""Linux tools - real host data only (spec sections 15, 16, 21).

Every function in this module answers from ``/proc``, ``/sys`` or a real
command.  Nothing here is ever derived from model knowledge, and each tool
returns the raw command output alongside the parsed structure so the answer can
be checked.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .registry import Level, ToolError, sh, tool

SERVICE_NAME_RE = r"[A-Za-z0-9@._\-]{1,64}"
PACKAGE_NAME_RE = r"[a-z0-9][a-z0-9+.\-]{1,64}"

PROTECTED_PATH_PREFIXES = (
    "/boot", "/dev", "/etc", "/proc", "/sys", "/usr", "/bin", "/sbin", "/lib",
    "/var/lib/rancher", "/etc/rancher", "/root/.ssh", "/home",
)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise ToolError("cannot read %s: %s" % (path, exc))


def _file_evidence(path: str) -> Dict[str, Any]:
    return {"command": "read %s" % path, "exit_code": 0, "stdout": _read(path)[-4000:], "stderr": ""}


# --------------------------------------------------------------------------
# System / OS
# --------------------------------------------------------------------------
@tool(
    name="get_os_info",
    description="Operating system distribution, version and codename from /etc/os-release.",
    category="linux",
    level=Level.READ_ONLY,
)
def get_os_info() -> Tuple[Dict[str, Any], List[Any]]:
    data: Dict[str, Any] = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.lower()] = value.strip().strip('"')
    return (
        {
            "distribution": data.get("name"),
            "version": data.get("version_id"),
            "codename": data.get("version_codename"),
            "pretty_name": data.get("pretty_name"),
            "id": data.get("id"),
        },
        [_file_evidence("/etc/os-release")],
    )


@tool(
    name="get_kernel",
    description="Kernel release, version and architecture (uname -a).",
    category="linux",
    level=Level.READ_ONLY,
)
def get_kernel() -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["uname", "-a"])
    parts = res.out.split()
    return (
        {
            "kernel_release": parts[2] if len(parts) > 2 else None,
            "architecture": parts[-2] if len(parts) > 2 else None,
            "full": res.out,
        },
        [res],
    )


@tool(
    name="get_uptime",
    description="How long the machine has been up, plus the boot timestamp.",
    category="linux",
    level=Level.READ_ONLY,
)
def get_uptime() -> Tuple[Dict[str, Any], List[Any]]:
    raw = _read("/proc/uptime").split()
    seconds = float(raw[0]) if raw else 0.0
    pretty = sh(["uptime", "-p"])
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return (
        {
            "uptime_seconds": int(seconds),
            "uptime_human": "%dd %dh %dm" % (days, hours, minutes),
            "uptime_pretty": pretty.out or None,
            "boot_time_epoch": int(time.time() - seconds),
        },
        [_file_evidence("/proc/uptime"), pretty],
    )


@tool(
    name="get_load",
    description="Load average over 1/5/15 minutes, normalised against CPU count.",
    category="linux",
    level=Level.READ_ONLY,
)
def get_load() -> Tuple[Dict[str, Any], List[Any]]:
    fields = _read("/proc/loadavg").split()
    cores = os.cpu_count() or 1
    load1, load5, load15 = (float(fields[i]) for i in range(3))
    return (
        {
            "load_1m": load1,
            "load_5m": load5,
            "load_15m": load15,
            "cpu_cores": cores,
            "load_per_core_1m": round(load1 / cores, 2),
            "pressure": "high" if load1 / cores > 1.0 else "normal",
            "running_processes": fields[3] if len(fields) > 3 else None,
        },
        [_file_evidence("/proc/loadavg")],
    )


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------
def _cpu_sample() -> Optional[List[int]]:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            return [int(value) for value in line.split()[1:]]
    return None


@tool(
    name="get_cpu_info",
    description="CPU model, core count and real current utilisation measured from /proc/stat.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "sample_seconds": {"type": "number", "default": 0.5, "minimum": 0.1, "maximum": 5}
        },
        "required": [],
    },
)
def get_cpu_info(sample_seconds: float = 0.5) -> Tuple[Dict[str, Any], List[Any]]:
    first = _cpu_sample()
    time.sleep(max(0.1, min(float(sample_seconds), 5.0)))
    second = _cpu_sample()
    usage_percent = None
    idle_percent = None
    if first and second:
        delta = [b - a for a, b in zip(first, second)]
        total = sum(delta)
        idle = delta[3] + (delta[4] if len(delta) > 4 else 0)
        if total > 0:
            usage_percent = round((total - idle) / total * 100, 1)
            idle_percent = round(idle / total * 100, 1)

    model = None
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break

    lscpu = sh(["lscpu"])
    details: Dict[str, str] = {}
    for line in lscpu.out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            details[key.strip()] = value.strip()

    return (
        {
            "model": model or details.get("Model name"),
            "logical_cores": os.cpu_count(),
            "architecture": details.get("Architecture"),
            "threads_per_core": details.get("Thread(s) per core"),
            "mhz": details.get("CPU MHz") or details.get("CPU max MHz"),
            "usage_percent": usage_percent,
            "idle_percent": idle_percent,
            "sampled_over_seconds": sample_seconds,
        },
        [_file_evidence("/proc/cpuinfo"), lscpu],
    )


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------
@tool(
    name="get_memory_info",
    description="RAM and swap usage read from /proc/meminfo and verified with `free -h`.",
    category="linux",
    level=Level.READ_ONLY,
)
def get_memory_info() -> Tuple[Dict[str, Any], List[Any]]:
    values: Dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0])

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = total - available
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    free_h = sh(["free", "-h"])

    return (
        {
            "total_mb": round(total / 1024),
            "used_mb": round(used / 1024),
            "available_mb": round(available / 1024),
            "free_mb": round(values.get("MemFree", 0) / 1024),
            "cached_mb": round(values.get("Cached", 0) / 1024),
            "buffers_mb": round(values.get("Buffers", 0) / 1024),
            "used_percent": round(used / total * 100, 1) if total else None,
            "swap_total_mb": round(swap_total / 1024),
            "swap_used_mb": round((swap_total - swap_free) / 1024),
            "swap_used_percent": round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0,
            "pressure": "high" if total and used / total > 0.9 else "normal",
        },
        [_file_evidence("/proc/meminfo"), free_h],
    )


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------
@tool(
    name="get_disk_info",
    description="Filesystem usage per mount point (df), excluding pseudo filesystems.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "maxLength": 256, "pattern": r"/[^\s;|&]*"}},
        "required": [],
    },
)
def get_disk_info(path: Optional[str] = None) -> Tuple[Dict[str, Any], List[Any]]:
    cmd = ["df", "-PB1", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs"]
    if path:
        cmd = ["df", "-PB1", path]
    res = sh(cmd)
    mounts: List[Dict[str, Any]] = []
    for line in res.out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total, used, avail = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        mounts.append(
            {
                "filesystem": parts[0],
                "mount": parts[5],
                "total_gb": round(total / 1024 ** 3, 1),
                "used_gb": round(used / 1024 ** 3, 1),
                "free_gb": round(avail / 1024 ** 3, 1),
                "used_percent": round(used / total * 100, 1) if total else None,
            }
        )
    root = next((m for m in mounts if m["mount"] == "/"), mounts[0] if mounts else None)
    pressure = [m["mount"] for m in mounts if (m["used_percent"] or 0) >= 85]
    return (
        {"mounts": mounts, "root": root, "mounts_over_85_percent": pressure},
        [res, sh(["df", "-h"])],
    )


# --------------------------------------------------------------------------
# Processes / services / ports
# --------------------------------------------------------------------------
@tool(
    name="get_processes",
    description="Top processes by CPU or memory, with PID, user, CPU%, MEM% and RSS.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "sort_by": {"type": "string", "enum": ["cpu", "memory"], "default": "cpu"},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "name_filter": {"type": "string", "maxLength": 64, "pattern": r"[A-Za-z0-9_.\-/]{0,64}"},
        },
        "required": [],
    },
)
def get_processes(sort_by: str = "cpu", limit: int = 10, name_filter: Optional[str] = None) -> Tuple[Dict[str, Any], List[Any]]:
    key = "-%cpu" if sort_by == "cpu" else "-%mem"
    res = sh(["ps", "-eo", "pid,user:16,pcpu,pmem,rss,etime,comm,args", "--sort", key, "--no-headers"], timeout=20)
    rows: List[Dict[str, Any]] = []
    for line in res.out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        command = parts[7] if len(parts) > 7 else parts[6]
        if name_filter and name_filter not in command and name_filter not in parts[6]:
            continue
        rows.append(
            {
                "pid": int(parts[0]),
                "user": parts[1],
                "cpu_percent": float(parts[2]),
                "memory_percent": float(parts[3]),
                "rss_mb": round(int(parts[4]) / 1024, 1),
                "elapsed": parts[5],
                "name": parts[6],
                "command": command[:200],
            }
        )
        if len(rows) >= limit:
            break
    return ({"sorted_by": sort_by, "count": len(rows), "processes": rows}, [res])


@tool(
    name="get_services",
    description="systemd services and their state. Use state=failed to find broken services.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "state": {"type": "string", "enum": ["all", "running", "failed"], "default": "running"},
            "limit": {"type": "integer", "default": 40, "minimum": 1, "maximum": 200},
        },
        "required": [],
    },
)
def get_services(state: str = "running", limit: int = 40) -> Tuple[Dict[str, Any], List[Any]]:
    cmd = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend", "--plain"]
    if state == "failed":
        cmd.append("--state=failed")
    elif state == "running":
        cmd.append("--state=running")
    else:
        cmd.append("--all")
    res = sh(cmd, timeout=30)
    services: List[Dict[str, Any]] = []
    for line in res.out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        services.append(
            {
                "unit": parts[0],
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    failed = [s for s in services if s["active"] == "failed" or s["sub"] == "failed"]
    return (
        {"state_filter": state, "count": len(services), "failed_count": len(failed), "failed": failed, "services": services[:limit]},
        [res],
    )


@tool(
    name="get_service_status",
    description="Detailed status of one systemd unit: active state, sub state, PID, memory and recent log lines.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": SERVICE_NAME_RE, "maxLength": 64}},
        "required": ["name"],
    },
)
def get_service_status(name: str) -> Tuple[Dict[str, Any], List[Any]]:
    unit = name if name.endswith((".service", ".socket", ".timer", ".target")) else name + ".service"
    show = sh(["systemctl", "show", unit, "--no-pager", "--property=ActiveState,SubState,LoadState,MainPID,MemoryCurrent,NRestarts,ExecMainStartTimestamp,Result,UnitFileState"], timeout=20)
    props: Dict[str, str] = {}
    for line in show.out.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    logs = sh(["journalctl", "-u", unit, "-n", "20", "--no-pager", "--output", "short-iso"], timeout=30)
    memory_bytes = props.get("MemoryCurrent")
    return (
        {
            "unit": unit,
            "exists": props.get("LoadState") not in (None, "not-found"),
            "active_state": props.get("ActiveState"),
            "sub_state": props.get("SubState"),
            "enabled": props.get("UnitFileState"),
            "main_pid": int(props["MainPID"]) if props.get("MainPID", "0").isdigit() else None,
            "memory_mb": round(int(memory_bytes) / 1024 ** 2, 1) if memory_bytes and memory_bytes.isdigit() else None,
            "restarts": props.get("NRestarts"),
            "result": props.get("Result"),
            "recent_log": logs.out[-3000:],
        },
        [show, logs],
    )


@tool(
    name="get_ports",
    description="Listening TCP/UDP sockets with the owning process (ss -tulpn).",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
        "required": [],
    },
)
def get_ports(port: Optional[int] = None) -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["ss", "-tulpnH"], timeout=20)
    listeners: List[Dict[str, Any]] = []
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[4]
        local_port = local.rsplit(":", 1)[-1]
        process = ""
        match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if match:
            process = "%s (pid %s)" % (match.group(1), match.group(2))
        entry = {
            "protocol": parts[0],
            "state": parts[1],
            "local_address": local,
            "port": int(local_port) if local_port.isdigit() else None,
            "process": process,
        }
        if port is None or entry["port"] == port:
            listeners.append(entry)
    return ({"count": len(listeners), "filter_port": port, "listeners": listeners}, [res])


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------
@tool(
    name="get_logs",
    description="Read recent system logs. source=system|auth|kernel|boot|unit. For unit, pass the unit name.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["system", "auth", "kernel", "boot", "unit"], "default": "system"},
            "unit": {"type": "string", "pattern": SERVICE_NAME_RE, "maxLength": 64},
            "lines": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            "grep": {"type": "string", "maxLength": 64, "pattern": r"[A-Za-z0-9 _.:\-/]{0,64}"},
        },
        "required": [],
    },
    timeout=45,
)
def get_logs(source: str = "system", unit: Optional[str] = None, lines: int = 50, grep: Optional[str] = None) -> Tuple[Dict[str, Any], List[Any]]:
    if source == "unit":
        if not unit:
            raise ToolError("source=unit requires the 'unit' argument")
        cmd = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output", "short-iso"]
    elif source == "kernel":
        cmd = ["journalctl", "-k", "-n", str(lines), "--no-pager", "--output", "short-iso"]
    elif source == "boot":
        cmd = ["journalctl", "-b", "-n", str(lines), "--no-pager", "--output", "short-iso"]
    elif source == "auth":
        cmd = ["journalctl", "-t", "sshd", "-t", "sudo", "-n", str(lines), "--no-pager", "--output", "short-iso"]
    else:
        cmd = ["journalctl", "-n", str(lines), "--no-pager", "--output", "short-iso"]
    if grep:
        cmd += ["--grep", grep]
    res = sh(cmd, timeout=45)
    if not res.ok and source in ("system", "auth", "kernel"):
        fallback = {"system": "/var/log/syslog", "auth": "/var/log/auth.log", "kernel": "/var/log/kern.log"}[source]
        if os.path.exists(fallback):
            res = sh(["tail", "-n", str(lines), fallback], timeout=30)
    body = res.out
    if grep and res.ok and "--grep" not in res.cmd:
        body = "\n".join(line for line in body.splitlines() if grep in line)
    return ({"source": source, "unit": unit, "lines": len(body.splitlines()), "log": body[-8000:]}, [res])


@tool(
    name="get_system_info",
    description="One-shot overview: OS, kernel, uptime, CPU, memory, disk and load. Use this for 'server status' questions.",
    category="linux",
    level=Level.READ_ONLY,
    timeout=45,
)
def get_system_info() -> Tuple[Dict[str, Any], List[Any]]:
    os_data, os_ev = get_os_info()
    kernel_data, kernel_ev = get_kernel()
    uptime_data, uptime_ev = get_uptime()
    cpu_data, cpu_ev = get_cpu_info(0.4)
    mem_data, mem_ev = get_memory_info()
    disk_data, disk_ev = get_disk_info()
    load_data, load_ev = get_load()
    return (
        {
            "hostname": sh(["hostname"]).out,
            "os": os_data,
            "kernel": kernel_data,
            "uptime": uptime_data,
            "cpu": cpu_data,
            "memory": mem_data,
            "disk": disk_data["root"],
            "disk_all": disk_data["mounts"],
            "load": load_data,
        },
        os_ev + kernel_ev + uptime_ev + cpu_ev + mem_ev + disk_ev + load_ev,
    )


@tool(
    name="get_network_info",
    description="Host network configuration: interfaces, addresses, default route and DNS servers.",
    category="linux",
    level=Level.READ_ONLY,
)
def get_network_info() -> Tuple[Dict[str, Any], List[Any]]:
    addr = sh(["ip", "-o", "-4", "addr", "show"])
    route = sh(["ip", "route", "show"])
    interfaces = []
    for line in addr.out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] != "lo":
            interfaces.append({"name": parts[1], "address": parts[3]})
    gateway = None
    match = re.search(r"default via (\S+) dev (\S+)", route.out)
    if match:
        gateway = {"via": match.group(1), "device": match.group(2)}
    dns = [line.split()[1] for line in _read("/etc/resolv.conf").splitlines() if line.startswith("nameserver")]
    return (
        {"hostname": sh(["hostname"]).out, "interfaces": interfaces, "default_gateway": gateway, "dns_servers": dns, "routes": route.out.splitlines()},
        [addr, route],
    )


# --------------------------------------------------------------------------
# Write-level tools
# --------------------------------------------------------------------------
def _policy() -> Dict[str, Any]:
    """Load the permission policy lazily; fall back to a conservative default."""
    try:
        from agent.permissions import policy_document

        return policy_document()
    except Exception:  # noqa: BLE001 - tools must still work standalone
        return {"safe_write_services": [], "critical_services": ["k3s", "ssh", "sshd"], "installable_packages": []}


@tool(
    name="restart_service",
    description="Restart a systemd service. Critical services are escalated to an approval prompt by the permission layer.",
    category="linux",
    level=Level.SAFE_WRITE,
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": SERVICE_NAME_RE, "maxLength": 64}},
        "required": ["name"],
    },
    timeout=90,
    risk_note="Restarting a service interrupts anything currently using it.",
)
def restart_service(name: str) -> Tuple[Dict[str, Any], List[Any]]:
    unit = name if "." in name else name + ".service"
    policy = _policy()
    if unit.replace(".service", "") in (policy.get("critical_services") or []):
        raise ToolError("%s is a protected critical service; it cannot be restarted by the agent" % unit)
    before, _ = get_service_status(unit)
    if not before["exists"]:
        raise ToolError("no such systemd unit: %s" % unit)
    action = sh(["systemctl", "restart", unit], timeout=90)
    time.sleep(2)
    after, after_ev = get_service_status(unit)
    return (
        {
            "unit": unit,
            "restarted": action.ok,
            "state_before": before["active_state"],
            "state_after": after["active_state"],
            "verified_healthy": after["active_state"] == "active",
            "recent_log": after["recent_log"][-1500:],
        },
        [action] + after_ev,
    )


@tool(
    name="stop_service",
    description="Stop a systemd service. Always requires approval.",
    category="linux",
    level=Level.MEDIUM_RISK,
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": SERVICE_NAME_RE, "maxLength": 64}},
        "required": ["name"],
    },
    timeout=90,
    risk_note="The service stays down until something starts it again.",
)
def stop_service(name: str) -> Tuple[Dict[str, Any], List[Any]]:
    unit = name if "." in name else name + ".service"
    policy = _policy()
    if unit.replace(".service", "") in (policy.get("critical_services") or []):
        raise ToolError("%s is a protected critical service; it cannot be stopped by the agent" % unit)
    action = sh(["systemctl", "stop", unit], timeout=90)
    after, after_ev = get_service_status(unit)
    return ({"unit": unit, "stopped": action.ok, "state_after": after["active_state"]}, [action] + after_ev)


@tool(
    name="check_package",
    description="Check whether an apt package is installed and which version.",
    category="linux",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": PACKAGE_NAME_RE, "maxLength": 64}},
        "required": ["name"],
    },
)
def check_package(name: str) -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\n", name], timeout=30)
    installed = res.ok and res.out.endswith("installed")
    version = res.out.split()[1] if installed and len(res.out.split()) > 1 else None
    # dpkg-query exits 1 for "not installed", which is a valid answer rather
    # than a tool failure, so the evidence row is normalised to success.
    evidence = {
        "command": res.display,
        "exit_code": 0,
        "stdout": res.stdout or ("%s is not installed" % name),
        "stderr": "" if not res.ok and "no packages found" in res.stderr else res.stderr,
        "duration_ms": res.duration_ms,
    }
    return ({"package": name, "installed": installed, "version": version}, [evidence])


@tool(
    name="install_package",
    description="Install an apt package from the policy allow-list. Requires approval.",
    category="linux",
    level=Level.MEDIUM_RISK,
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": PACKAGE_NAME_RE, "maxLength": 64}},
        "required": ["name"],
    },
    timeout=900,
    risk_note="Installs software and its dependencies from the configured apt repositories.",
)
def install_package(name: str) -> Tuple[Dict[str, Any], List[Any]]:
    allowed = _policy().get("installable_packages") or []
    if allowed and name not in allowed:
        raise ToolError(
            "package %r is not in policies/permissions.yaml -> installable_packages (%s)" % (name, ", ".join(allowed))
        )
    existing, _ = check_package(name)
    if existing["installed"]:
        return ({"package": name, "already_installed": True, "version": existing["version"], "changed": False}, [])
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    update = sh(["apt-get", "update", "-qq"], timeout=600, env=env)
    install = sh(["apt-get", "install", "-y", "--no-install-recommends", name], timeout=900, env=env)
    after, after_ev = check_package(name)
    if not after["installed"]:
        raise ToolError("apt reported failure installing %s: %s" % (name, (install.stderr or install.stdout)[-400:]))
    return (
        {"package": name, "installed": True, "version": after["version"], "changed": True},
        [update, install] + after_ev,
    )


@tool(
    name="delete_file",
    description="Delete a single file. High risk: always requires typed confirmation and refuses system paths.",
    category="linux",
    level=Level.HIGH_RISK,
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "maxLength": 512, "pattern": r"/[^\s;|&]{1,511}"}},
        "required": ["path"],
    },
    risk_note="The file is removed permanently; there is no undo.",
    autonomous=False,
)
def delete_file(path: str) -> Tuple[Dict[str, Any], List[Any]]:
    real = os.path.realpath(path)
    if real == "/" or any(real == p or real.startswith(p + "/") for p in PROTECTED_PATH_PREFIXES):
        raise ToolError("refusing to delete inside a protected system path: %s" % real)
    if os.path.isdir(real):
        raise ToolError("%s is a directory; this tool only deletes single files" % real)
    if not os.path.exists(real):
        raise ToolError("no such file: %s" % real)
    size = os.path.getsize(real)
    os.remove(real)
    return (
        {"path": real, "deleted": not os.path.exists(real), "size_bytes": size},
        [{"command": "unlink %s" % real, "exit_code": 0, "stdout": "removed %d bytes" % size, "stderr": ""}],
    )
