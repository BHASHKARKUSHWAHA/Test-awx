"""Networking tools (spec section 35 and the Network Agent role in section 27)."""

from __future__ import annotations

import re
import socket
from typing import Any, Dict, List, Optional, Tuple

from .registry import Level, ToolError, sh, tool

HOST_RE = r"[A-Za-z0-9_.\-]{1,253}"
IP_OR_HOST_RE = r"[A-Za-z0-9_.:\-]{1,253}"


@tool(
    name="ping_host",
    description="ICMP reachability and round-trip time to a host.",
    category="networking",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "host": {"type": "string", "pattern": IP_OR_HOST_RE, "maxLength": 253},
            "count": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
        },
        "required": ["host"],
    },
    timeout=40,
)
def ping_host(host: str, count: int = 3) -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["ping", "-c", str(count), "-W", "2", host], timeout=40)
    loss = None
    rtt = None
    match = re.search(r"(\d+(?:\.\d+)?)% packet loss", res.stdout)
    if match:
        loss = float(match.group(1))
    match = re.search(r"min/avg/max/\S+ = [\d.]+/([\d.]+)/", res.stdout)
    if match:
        rtt = float(match.group(1))
    return (
        {"host": host, "reachable": res.ok and (loss is None or loss < 100), "packet_loss_percent": loss, "avg_rtt_ms": rtt},
        [res],
    )


@tool(
    name="dns_lookup",
    description="Resolve a hostname using the machine's configured resolvers.",
    category="networking",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"hostname": {"type": "string", "pattern": HOST_RE, "maxLength": 253}},
        "required": ["hostname"],
    },
    timeout=30,
)
def dns_lookup(hostname: str) -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["getent", "hosts", hostname], timeout=20)
    addresses = [line.split()[0] for line in res.out.splitlines() if line.split()]
    evidence: List[Any] = [res]
    if not addresses:
        dig = sh(["dig", "+short", hostname], timeout=20)
        evidence.append(dig)
        addresses = [a for a in dig.out.splitlines() if a and not a.endswith(".")]
    return ({"hostname": hostname, "resolved": bool(addresses), "addresses": addresses}, evidence)


@tool(
    name="check_port",
    description="Check whether a TCP port on a host accepts connections.",
    category="networking",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "host": {"type": "string", "pattern": IP_OR_HOST_RE, "maxLength": 253},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "timeout_seconds": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        },
        "required": ["host", "port"],
    },
    timeout=30,
)
def check_port(host: str, port: int, timeout_seconds: int = 5) -> Tuple[Dict[str, Any], List[Any]]:
    open_port = False
    error = ""
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            open_port = True
    except OSError as exc:
        error = str(exc)
    return (
        {"host": host, "port": port, "open": open_port, "error": error},
        [{"command": "tcp connect %s:%d" % (host, port), "exit_code": 0 if open_port else 1, "stdout": "open" if open_port else "", "stderr": error}],
    )


@tool(
    name="get_connections",
    description="Established TCP connections, optionally filtered by port.",
    category="networking",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 200},
        },
        "required": [],
    },
    timeout=30,
)
def get_connections(port: Optional[int] = None, limit: int = 30) -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["ss", "-tnH", "state", "established"], timeout=25)
    rows = []
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local, peer = parts[-2], parts[-1]
        local_port = local.rsplit(":", 1)[-1]
        if port and local_port != str(port) and peer.rsplit(":", 1)[-1] != str(port):
            continue
        rows.append({"local": local, "peer": peer})
        if len(rows) >= limit:
            break
    return ({"count": len(rows), "filter_port": port, "connections": rows}, [res])


@tool(
    name="get_routes",
    description="Kernel routing table and the default route.",
    category="networking",
    level=Level.READ_ONLY,
    timeout=25,
)
def get_routes() -> Tuple[Dict[str, Any], List[Any]]:
    res = sh(["ip", "route", "show"], timeout=20)
    default = None
    match = re.search(r"default via (\S+) dev (\S+)", res.out)
    if match:
        default = {"via": match.group(1), "device": match.group(2)}
    return ({"default_route": default, "routes": res.out.splitlines()}, [res])


@tool(
    name="get_firewall_status",
    description="Firewall state: ufw status plus whether the stack's NodePorts are reachable locally.",
    category="networking",
    level=Level.READ_ONLY,
    timeout=40,
)
def get_firewall_status() -> Tuple[Dict[str, Any], List[Any]]:
    ufw = sh(["ufw", "status", "verbose"], timeout=20)
    active = ufw.ok and "Status: active" in ufw.stdout
    rules = [line.strip() for line in ufw.out.splitlines() if "ALLOW" in line or "DENY" in line]
    ports: Dict[str, bool] = {}
    evidence: List[Any] = [ufw]
    for port in (30080, 30081, 6443):
        data, port_ev = check_port("127.0.0.1", port, 3)
        ports[str(port)] = data["open"]
        evidence += port_ev
    return (
        {"ufw_available": ufw.code != 127, "ufw_active": active, "rules": rules[:20], "local_ports_open": ports},
        evidence,
    )


@tool(
    name="disable_firewall",
    description="Disable the ufw firewall. High risk: exposes every listening port on this machine.",
    category="networking",
    level=Level.HIGH_RISK,
    timeout=60,
    risk_note="All firewall filtering stops; every listening service becomes reachable from the network.",
    autonomous=False,
)
def disable_firewall() -> Tuple[Dict[str, Any], List[Any]]:
    before = sh(["ufw", "status"], timeout=20)
    if before.code == 127:
        raise ToolError("ufw is not installed on this machine")
    action = sh(["ufw", "--force", "disable"], timeout=40)
    after = sh(["ufw", "status"], timeout=20)
    return (
        {"disabled": action.ok, "status_before": before.out.splitlines()[0] if before.out else "", "status_after": after.out.splitlines()[0] if after.out else ""},
        [before, action, after],
    )
