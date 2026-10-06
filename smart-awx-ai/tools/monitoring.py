"""Monitoring tools - cross-layer health (spec sections 30, 40, 47).

These compose the lower level tools so a single question like "give me a
complete health report" produces one grounded answer instead of six.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from . import awx as awx_tools
from . import kubernetes as k8s_tools
from . import linux as linux_tools
from .registry import Level, ToolError, sh, tool

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def _ollama_status() -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        models = [m.get("name") for m in payload.get("models", [])]
        return {"reachable": True, "models": models, "model_count": len(models), "url": OLLAMA_URL}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"reachable": False, "error": str(exc), "url": OLLAMA_URL, "models": []}


@tool(
    name="get_ai_runtime_status",
    description="Status of the local AI runtime (Ollama): reachable, which models are present, and the configured model.",
    category="monitoring",
    level=Level.READ_ONLY,
    timeout=30,
)
def get_ai_runtime_status() -> Tuple[Dict[str, Any], List[Any]]:
    status = _ollama_status()
    configured = os.environ.get("AI_MODEL")
    status["configured_model"] = configured
    status["configured_model_present"] = bool(configured and any((m or "").startswith(configured.split(":")[0]) and m == configured for m in status.get("models", [])))
    service = linux_tools.get_service_status("ollama")[0] if os.path.exists("/run/systemd/system") else {}
    status["service_state"] = service.get("active_state")
    return (
        status,
        [{"command": "GET %s/api/tags" % OLLAMA_URL, "exit_code": 0 if status["reachable"] else 1, "stdout": json.dumps(status.get("models"))[:1000], "stderr": status.get("error", "")}],
    )


@tool(
    name="get_resource_summary",
    description="Compact CPU / RAM / disk / load snapshot with a verdict for each dimension.",
    category="monitoring",
    level=Level.READ_ONLY,
    timeout=45,
)
def get_resource_summary() -> Tuple[Dict[str, Any], List[Any]]:
    cpu, cpu_ev = linux_tools.get_cpu_info(0.4)
    mem, mem_ev = linux_tools.get_memory_info()
    disk, disk_ev = linux_tools.get_disk_info()
    load, load_ev = linux_tools.get_load()

    def verdict(value: Any, warn: float, crit: float) -> str:
        if value is None:
            return "unknown"
        if value >= crit:
            return "critical"
        if value >= warn:
            return "warning"
        return "ok"

    root = disk["root"] or {}
    return (
        {
            "cpu_percent": cpu["usage_percent"],
            "cpu_verdict": verdict(cpu["usage_percent"], 80, 95),
            "memory_percent": mem["used_percent"],
            "memory_used_mb": mem["used_mb"],
            "memory_total_mb": mem["total_mb"],
            "memory_verdict": verdict(mem["used_percent"], 85, 95),
            "disk_percent": root.get("used_percent"),
            "disk_free_gb": root.get("free_gb"),
            "disk_verdict": verdict(root.get("used_percent"), 80, 90),
            "load_1m": load["load_1m"],
            "load_per_core": load["load_per_core_1m"],
            "load_verdict": verdict(load["load_per_core_1m"], 1.0, 2.0),
            "swap_used_percent": mem["swap_used_percent"],
        },
        cpu_ev + mem_ev + disk_ev + load_ev,
    )


@tool(
    name="get_top_consumers",
    description="The processes currently using the most CPU and the most memory.",
    category="monitoring",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20}}, "required": []},
    timeout=30,
)
def get_top_consumers(limit: int = 5) -> Tuple[Dict[str, Any], List[Any]]:
    by_cpu, cpu_ev = linux_tools.get_processes("cpu", limit)
    by_mem, mem_ev = linux_tools.get_processes("memory", limit)
    return ({"top_cpu": by_cpu["processes"], "top_memory": by_mem["processes"]}, cpu_ev + mem_ev)


@tool(
    name="get_pod_restart_summary",
    description="Pods that are restarting across the cluster, with restart counts and failure reasons.",
    category="monitoring",
    level=Level.READ_ONLY,
    timeout=45,
)
def get_pod_restart_summary() -> Tuple[Dict[str, Any], List[Any]]:
    pods, evidence = k8s_tools.get_kubernetes_pods(all_namespaces=True)
    restarting = sorted(
        [p for p in pods["pods"] if (p.get("restarts") or 0) > 0],
        key=lambda p: p["restarts"],
        reverse=True,
    )
    crashloop = [p for p in restarting if "CrashLoopBackOff" in (p.get("problem_reasons") or [])]
    return (
        {"restarting_count": len(restarting), "crashloop_count": len(crashloop), "crashloop": crashloop, "restarting": restarting[:15]},
        evidence,
    )


@tool(
    name="get_health_report",
    description="Full stack health report: system resources, K3s, Kubernetes workloads, AWX and the AI runtime. Use this for 'complete health report' questions.",
    category="monitoring",
    level=Level.READ_ONLY,
    timeout=90,
)
def get_health_report() -> Tuple[Dict[str, Any], List[Any]]:
    evidence: List[Any] = []
    report: Dict[str, Any] = {}

    resources, res_ev = get_resource_summary()
    evidence += res_ev
    report["system"] = resources

    k3s_state = sh(["systemctl", "is-active", "k3s"], timeout=15)
    report["k3s"] = {"service_state": k3s_state.out or "unknown", "active": k3s_state.out == "active"}
    evidence.append(k3s_state)

    try:
        cluster, k8s_ev = k8s_tools.get_kubernetes_health()
        evidence += k8s_ev
        report["kubernetes"] = cluster
    except ToolError as exc:
        report["kubernetes"] = {"status": "unavailable", "error": str(exc)}

    try:
        awx_health, awx_ev = awx_tools.get_awx_health()
        evidence += awx_ev
        report["awx"] = awx_health
        jobs, jobs_ev = awx_tools.list_awx_jobs("failed", 5)
        evidence += jobs_ev
        report["awx"]["failed_jobs"] = jobs["jobs"]
        report["awx"]["failed_job_count"] = jobs["count"]
    except ToolError as exc:
        report["awx"] = {"status": "unavailable", "error": str(exc)}

    ai_status, ai_ev = get_ai_runtime_status()
    evidence += ai_ev
    report["ai_runtime"] = ai_status

    problems: List[str] = []
    if resources["memory_verdict"] in ("warning", "critical"):
        problems.append("memory at %s%%" % resources["memory_percent"])
    if resources["disk_verdict"] in ("warning", "critical"):
        problems.append("disk at %s%%" % resources["disk_percent"])
    if resources["cpu_verdict"] in ("warning", "critical"):
        problems.append("cpu at %s%%" % resources["cpu_percent"])
    if not report["k3s"]["active"]:
        problems.append("k3s service is %s" % report["k3s"]["service_state"])
    if report.get("kubernetes", {}).get("unhealthy_pod_count"):
        problems.append("%d unhealthy pod(s)" % report["kubernetes"]["unhealthy_pod_count"])
    if report.get("awx", {}).get("status") != "healthy":
        problems.append("awx is %s" % report.get("awx", {}).get("status", "unavailable"))
    if report.get("awx", {}).get("failed_job_count"):
        problems.append("%d failed AWX job(s)" % report["awx"]["failed_job_count"])
    if not ai_status.get("reachable"):
        problems.append("AI runtime unreachable")

    report["overall_status"] = "healthy" if not problems else "degraded"
    report["problems"] = problems
    return (report, evidence)
