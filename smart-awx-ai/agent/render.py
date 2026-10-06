"""Deterministic renderers.

These turn tool output into readable text **without** an LLM.  They exist so
the agent stays useful (and still truthful) when the local model is down, and
so the model always has a compact, factual context to summarise rather than
raw JSON blobs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.registry import ToolResult


def _pct(value: Any) -> str:
    return "%s%%" % value if value is not None else "unknown"


def summarise_tool(result: ToolResult) -> str:
    """One short line describing what a tool established."""
    if not result.ok:
        return "%s failed: %s" % (result.tool, result.error or "unknown error")
    data = result.data
    name = result.tool
    if not isinstance(data, dict):
        return "%s returned: %s" % (name, str(data)[:200])

    if name == "get_memory_info":
        return "RAM %s/%s MB used (%s), swap %s MB used" % (data.get("used_mb"), data.get("total_mb"), _pct(data.get("used_percent")), data.get("swap_used_mb"))
    if name == "get_cpu_info":
        return "CPU %s, %s cores, %s in use" % (data.get("model"), data.get("logical_cores"), _pct(data.get("usage_percent")))
    if name == "get_disk_info":
        root = data.get("root") or {}
        return "Disk / %s GB free of %s GB (%s used)" % (root.get("free_gb"), root.get("total_gb"), _pct(root.get("used_percent")))
    if name == "get_load":
        return "Load %s / %s / %s on %s cores" % (data.get("load_1m"), data.get("load_5m"), data.get("load_15m"), data.get("cpu_cores"))
    if name == "get_uptime":
        return "Uptime %s" % data.get("uptime_human")
    if name == "get_processes":
        top = (data.get("processes") or [{}])[0]
        return "%d processes listed, top by %s is %s (%s%% cpu)" % (data.get("count", 0), data.get("sorted_by"), top.get("name"), top.get("cpu_percent"))
    if name == "get_services":
        return "%d services (%d failed)" % (data.get("count", 0), data.get("failed_count", 0))
    if name == "get_service_status":
        return "%s is %s/%s" % (data.get("unit"), data.get("active_state"), data.get("sub_state"))
    if name == "get_ports":
        return "%d listening sockets" % data.get("count", 0)
    if name in ("get_kubernetes_pods",):
        return "%d pods, %d unhealthy" % (data.get("count", 0), data.get("unhealthy_count", 0))
    if name == "get_kubernetes_nodes":
        return "%d node(s), all ready: %s" % (data.get("count", 0), data.get("all_ready"))
    if name == "get_kubernetes_health":
        return "Cluster %s, %d unhealthy pod(s)" % (data.get("status"), data.get("unhealthy_pod_count", 0))
    if name == "get_kubernetes_events":
        return "%d events, %d warnings" % (data.get("count", 0), len(data.get("warnings") or []))
    if name == "get_kubernetes_logs":
        return "read %d log lines from %s" % (data.get("lines", 0), data.get("pod"))
    if name == "describe_pod":
        summary = data.get("summary") or {}
        return "pod %s is %s (%s), restarts=%s, OOMKilled=%s" % (data.get("pod"), summary.get("phase"), summary.get("ready"), summary.get("restarts"), data.get("oom_killed"))
    if name == "get_awx_health":
        return "AWX %s (version %s, authenticated=%s)" % (data.get("status"), data.get("version"), data.get("authenticated"))
    if name in ("list_awx_jobs",):
        return "%d jobs, %d failed" % (data.get("count", 0), data.get("failed_count", 0))
    if name == "get_awx_failed_jobs":
        return "%d failed AWX job(s)" % data.get("count", 0)
    if name == "list_awx_job_templates":
        return "%d job template(s)" % data.get("count", 0)
    if name == "list_awx_projects":
        return "%d project(s)" % data.get("count", 0)
    if name == "get_health_report":
        return "overall %s; problems: %s" % (data.get("overall_status"), ", ".join(data.get("problems") or []) or "none")
    if name == "get_resource_summary":
        return "cpu %s, memory %s, disk %s" % (_pct(data.get("cpu_percent")), _pct(data.get("memory_percent")), _pct(data.get("disk_percent")))
    if name == "get_ai_runtime_status":
        return "AI runtime reachable=%s, models=%s" % (data.get("reachable"), ", ".join(data.get("models") or []) or "none")
    if name == "restart_service":
        return "%s restarted: %s -> %s" % (data.get("unit"), data.get("state_before"), data.get("state_after"))
    if name == "restart_pod":
        return "pod %s deleted, replacement healthy=%s" % (data.get("deleted_pod"), data.get("verified_healthy"))
    if name == "install_package":
        return "%s installed=%s version=%s" % (data.get("package"), data.get("installed"), data.get("version"))
    if name == "check_package":
        return "%s installed=%s" % (data.get("package"), data.get("installed"))
    if name == "launch_job_template":
        return "job %s launched, final status %s" % (data.get("job_id"), data.get("final_status"))
    keys = ", ".join(list(data)[:6])
    return "%s returned fields: %s" % (name, keys)


def facts_block(results: List[ToolResult]) -> str:
    """Compact, truthful context handed to the model for summarisation."""
    chunks: List[str] = []
    for result in results:
        header = "TOOL %s (%s)" % (result.tool, "ok" if result.ok else "FAILED")
        if not result.ok:
            chunks.append("%s\nerror: %s" % (header, result.error))
            continue
        body = json.dumps(result.data, default=str, ensure_ascii=False)
        if len(body) > 3000:
            body = body[:3000] + " ...(truncated)"
        chunks.append("%s\n%s" % (header, body))
    return "\n\n".join(chunks)


def plain_answer(intent: str, results: List[ToolResult]) -> str:
    """Readable answer built only from tool output - used when the LLM is down."""
    lines: List[str] = []
    for result in results:
        if result.ok and result.tool == "get_health_report" and isinstance(result.data, dict):
            return health_report_text(result.data)
        lines.append(("- " if result.ok else "- [failed] ") + summarise_tool(result))
    if not lines:
        return "No tool produced output for this request."
    header = {
        "memory": "Memory, read from this machine:",
        "cpu": "CPU, measured on this machine:",
        "disk": "Disk usage, read from this machine:",
    }.get(intent, "Here is what I read from the machine:")
    return header + "\n" + "\n".join(lines)


def health_report_text(report: Dict[str, Any]) -> str:
    system = report.get("system") or {}
    kubernetes = report.get("kubernetes") or {}
    awx = report.get("awx") or {}
    ai = report.get("ai_runtime") or {}
    rows = [
        ("CPU", _pct(system.get("cpu_percent"))),
        ("RAM", "%s (%s / %s MB)" % (_pct(system.get("memory_percent")), system.get("memory_used_mb"), system.get("memory_total_mb"))),
        ("Disk", "%s (%s GB free)" % (_pct(system.get("disk_percent")), system.get("disk_free_gb"))),
        ("Load", "%s per core" % system.get("load_per_core")),
        ("K3s", (report.get("k3s") or {}).get("service_state", "unknown")),
        ("Kubernetes", kubernetes.get("status", "unavailable")),
        ("AWX", awx.get("status", "unavailable")),
        ("Failed pods", kubernetes.get("unhealthy_pod_count", "unknown")),
        ("Failed AWX jobs", awx.get("failed_job_count", "unknown")),
        ("AI runtime", "reachable" if ai.get("reachable") else "unreachable"),
    ]
    width = max(len(name) for name, _ in rows)
    body = "\n".join("%-*s : %s" % (width, name, value) for name, value in rows)
    problems = report.get("problems") or []
    verdict = "Overall: %s" % str(report.get("overall_status", "unknown")).upper()
    if problems:
        verdict += "\nProblems found:\n" + "\n".join("  - %s" % p for p in problems)
    return body + "\n\n" + verdict
