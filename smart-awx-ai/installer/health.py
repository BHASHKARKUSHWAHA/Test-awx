"""Final health check and report (spec sections 47 and 40).

Every line in the report comes from a probe executed at report time.  A
component that cannot be verified is reported as UNKNOWN, never as HEALTHY.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from . import ai as ai_phase
from . import awx as awx_phase
from . import kubernetes as k8s_phase
from .common import AGENT_PORT, AGENT_SERVICE, C, Log, State, run, which

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"
MISSING = "MISSING"


@dataclass
class Row:
    group: str
    name: str
    status: str
    detail: str = ""


def _service_state(unit: str) -> str:
    res = run(["systemctl", "is-active", unit], mutating=False, timeout=20)
    return res.out or "unknown"


def _http(url: str, timeout: int = 10) -> Optional[int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def collect(state: State, hardware=None) -> List[Row]:
    rows: List[Row] = []
    facts = state.facts

    # ---- system ---------------------------------------------------------
    if hardware is None:
        from . import hardware as hardware_module

        memory = hardware_module.detect_memory()
        disk = hardware_module.detect_disk()
        cpu = hardware_module.detect_cpu()
        os_info = hardware_module.detect_os()
    else:
        memory, disk, cpu, os_info = hardware.memory, hardware.disk, hardware.cpu, hardware.os

    rows.append(Row("SYSTEM", "OS", HEALTHY, os_info.get("pretty") or "unknown"))
    load_ratio = (cpu.get("load_1m") or 0) / max(cpu.get("logical_cores") or 1, 1)
    rows.append(Row("SYSTEM", "CPU", HEALTHY if load_ratio < 2 else DEGRADED, "%s cores, load/core %.2f" % (cpu.get("logical_cores"), load_ratio)))
    memory_ok = (memory.get("used_percent") or 0) < 92
    rows.append(Row("SYSTEM", "RAM", HEALTHY if memory_ok else DEGRADED, "%s / %s MB used (%s%%)" % (memory.get("used_mb"), memory.get("total_mb"), memory.get("used_percent"))))
    disk_ok = (disk.get("used_percent") or 0) < 90
    rows.append(Row("SYSTEM", "DISK", HEALTHY if disk_ok else DEGRADED, "%s GB free of %s GB" % (disk.get("free_gb"), disk.get("total_gb"))))

    # ---- kubernetes -----------------------------------------------------
    k3s_state = _service_state("k3s")
    rows.append(Row("KUBERNETES", "K3s", HEALTHY if k3s_state == "active" else (MISSING if not which("k3s") else DEGRADED), "systemd unit: %s" % k3s_state))
    if which("k3s") or which("kubectl"):
        checks = k8s_phase.verify()
        rows.append(Row("KUBERNETES", "Node", HEALTHY if checks["node_ready"] else DEGRADED, "Ready" if checks["node_ready"] else "not Ready"))
        rows.append(Row("KUBERNETES", "API", HEALTHY if checks["api_reachable"] else DEGRADED, "/readyz" if checks["api_reachable"] else "unreachable"))
        rows.append(Row("KUBERNETES", "CoreDNS", HEALTHY if checks["coredns_ready"] else DEGRADED, "ready" if checks["coredns_ready"] else "not ready"))
        rows.append(Row("KUBERNETES", "Storage", HEALTHY if checks["storage_class"] else DEGRADED, "storage class present" if checks["storage_class"] else "no storage class"))
    else:
        rows.append(Row("KUBERNETES", "Node", MISSING, "k3s is not installed"))

    # ---- AWX ------------------------------------------------------------
    awx_nodeport = facts.get("awx_nodeport", 30081)
    awx_url = facts.get("awx_url") or "http://127.0.0.1:%s" % awx_nodeport
    if which("k3s") or which("kubectl"):
        operator_ready = awx_phase.operator_deployment_ready()
        rows.append(Row("AWX", "Operator", HEALTHY if operator_ready else DEGRADED, facts.get("operator_version", "")))
        awx_ready = awx_phase.awx_ready()
        rows.append(Row("AWX", "AWX", HEALTHY if awx_ready else DEGRADED, "web and task deployments ready" if awx_ready else "deployments not ready"))
        code = _http(awx_url.rstrip("/") + "/api/v2/ping/", timeout=15)
        rows.append(Row("AWX", "AWX UI/API", HEALTHY if code and 200 <= code < 400 else DEGRADED, "HTTP %s on %s" % (code, awx_url)))
        rows.append(Row("AWX", "Admin secret", HEALTHY if awx_phase.admin_password() else DEGRADED, "retrieved from Kubernetes" if awx_phase.admin_password() else "not available"))
    else:
        rows.append(Row("AWX", "Operator", MISSING, "no cluster"))

    # ---- AI -------------------------------------------------------------
    ollama_state = _service_state("ollama")
    runtime_ok = ai_phase.ollama_reachable()
    rows.append(Row("AI", "AI Runtime", HEALTHY if runtime_ok else (MISSING if not which("ollama") else DEGRADED), "ollama %s, API %s" % (ollama_state, "up" if runtime_ok else "down")))
    models = ai_phase.installed_models()
    configured = facts.get("ai_model")
    model_ok = bool(models) and (configured in models if configured else True)
    rows.append(Row("AI", "AI Model", HEALTHY if model_ok else DEGRADED, "%s%s" % (configured or "none configured", "" if model_ok else " (not present: %s)" % ", ".join(models) if models else " (no models installed)")))

    agent_state = _service_state(AGENT_SERVICE)
    health = ai_phase.agent_health_payload()
    rows.append(Row("AI", "AI Backend", HEALTHY if health else DEGRADED, "unit %s, /health %s" % (agent_state, (health or {}).get("status", "unreachable"))))

    proxy_nodeport = facts.get("proxy_nodeport", 30080)
    ui_code = _http("http://127.0.0.1:%s/ai-agent/" % proxy_nodeport, timeout=15)
    rows.append(Row("AI", "AI UI", HEALTHY if ui_code and 200 <= ui_code < 400 else DEGRADED, "HTTP %s via proxy :%s" % (ui_code, proxy_nodeport)))

    # ---- integration ----------------------------------------------------
    components = (health or {}).get("components", {})
    for label, key in (("AWX API", "awx_api"), ("Kubernetes", "kubernetes_api"), ("Linux Tools", "linux_tools"), ("AI Model link", "ai_model")):
        component = components.get(key)
        if component:
            rows.append(Row("INTEGRATION", label, HEALTHY if component["status"] == "healthy" else DEGRADED, component.get("detail", "")))
        else:
            rows.append(Row("INTEGRATION", label, UNKNOWN, "agent backend did not report"))

    injected = (facts.get("integration") or {}).get("awx_html_contains_injection")
    rows.append(
        Row(
            "INTEGRATION",
            "AWX dashboard entry",
            HEALTHY if injected else (DEGRADED if injected is False else UNKNOWN),
            "script tag injected into the AWX page" if injected else "launcher only - open /ai-agent/ directly",
        )
    )
    tool_count = _tool_count() if health else 0
    rows.append(
        Row(
            "INTEGRATION",
            "AI Agent",
            HEALTHY if (health and tool_count > 0) else DEGRADED,
            "%d tools registered" % tool_count if tool_count else "backend unreachable",
        )
    )
    return rows


def _tool_count() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/status" % AGENT_PORT, timeout=10) as response:
            return int(json.loads(response.read().decode()).get("tools", 0))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return 0


def overall(rows: List[Row]) -> str:
    if any(row.status in (DEGRADED, MISSING) for row in rows):
        return DEGRADED
    if any(row.status == UNKNOWN for row in rows):
        return UNKNOWN
    return HEALTHY


def render(rows: List[Row], state: State) -> str:
    facts = state.facts
    host = facts.get("host_ip", "SERVER_IP")
    width = max(len(row.name) for row in rows) + 2
    lines = ["=" * 72, "SMART AWX + AI AGENT", "=" * 72, ""]
    current_group = None
    for row in rows:
        if row.group != current_group:
            if current_group is not None:
                lines.append("")
            current_group = row.group
            lines.append(current_group)
        colour = {HEALTHY: C.GREEN, DEGRADED: C.YELLOW, UNKNOWN: C.YELLOW, MISSING: C.RED}[row.status]
        lines.append("  %-*s : %s  %s" % (width, row.name, C.p(colour, "%-8s" % row.status), row.detail))
    lines.append("=" * 72)
    lines.append("")
    lines.append("AWX + AI Agent:")
    lines.append("  http://%s:%s" % (host, facts.get("proxy_nodeport", 30080)))
    lines.append("")
    lines.append("AI Agent UI (direct):")
    lines.append("  http://%s:%s/ai-agent/" % (host, facts.get("proxy_nodeport", 30080)))
    lines.append("")
    lines.append("AWX without the AI proxy:")
    lines.append("  http://%s:%s" % (host, facts.get("awx_nodeport", 30081)))
    lines.append("")
    lines.append("AWX login:")
    lines.append("  user     : admin")
    lines.append("  password : kubectl -n awx get secret awx-admin-password -o jsonpath='{.data.password}' | base64 -d")
    lines.append("")
    lines.append("AI Agent: open AWX and use the robot entry in the left navigation,")
    lines.append("          the floating launcher, or press Alt+A.")
    lines.append("")
    lines.append("Local break-glass account (only needed when AWX itself is down):")
    lines.append("  /etc/smart-awx-ai/credentials.txt  (root only)")
    lines.append("")
    lines.append("Model: %s   Profile: %s" % (facts.get("ai_model", "unknown"), facts.get("profile", "unknown")))
    lines.append("=" * 72)
    return "\n".join(lines)


def print_report(state: State, hardware=None) -> str:
    rows = collect(state, hardware)
    report = render(rows, state)
    print("\n" + report)
    status = overall(rows)
    if status == HEALTHY:
        Log.ok("All components reported healthy.")
    else:
        Log.warn("Overall status: %s - see the rows above." % status)
    return status
