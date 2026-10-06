"""Planner (spec sections 19, 26, 27).

The planner turns a natural-language request into an ordered list of *declared
tool calls*.  It never produces a shell command, and it never answers a factual
question itself.

Two tiers, in this order:

1. **Deterministic router** - regular expressions over English and Hinglish
   phrasing.  This is what makes "RAM kitni hai?" reliably run
   ``get_memory_info`` on a 3B local model, and it works even when the LLM is
   down.
2. **LLM tool selection** - for anything the router does not recognise, the
   model is asked to pick tools *from the registry catalogue* and return JSON.
   Unknown tool names are discarded, so a hallucinated tool can never execute.

If both tiers fail the planner falls back to a broad read-only inspection and
says so, rather than inventing an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tools import registry

from .llm import LLMUnavailable, extract_json
from .llm import client as llm_client

# --------------------------------------------------------------------------
# Specialised workers (spec section 27).  These are routing labels over the
# shared tool registry, not separate LLM processes - an 8 GB box cannot afford
# six models, and it does not need them.
# --------------------------------------------------------------------------
SPECIALISTS = {
    "linux": {"name": "Linux Agent", "icon": "\U0001F427"},
    "kubernetes": {"name": "Kubernetes Agent", "icon": "☸"},
    "awx": {"name": "AWX Agent", "icon": "⚙"},
    "networking": {"name": "Network Agent", "icon": "\U0001F310"},
    "monitoring": {"name": "Monitoring Agent", "icon": "\U0001F4CA"},
    "security": {"name": "Security Agent", "icon": "\U0001F510"},
}
ORCHESTRATOR = {"name": "AI Orchestrator", "icon": "\U0001F916"}


@dataclass
class Step:
    index: int
    description: str
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    specialist: str = "linux"
    status: str = "pending"  # pending | running | done | failed | blocked | awaiting_approval
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "tool": self.tool,
            "args": self.args,
            "specialist": SPECIALISTS.get(self.specialist, SPECIALISTS["linux"])["name"],
            "specialist_icon": SPECIALISTS.get(self.specialist, SPECIALISTS["linux"])["icon"],
            "status": self.status,
            "note": self.note,
        }


@dataclass
class Plan:
    intent: str
    goal: str
    steps: List[Step] = field(default_factory=list)
    strategy: str = "rule"  # rule | llm | fallback | conversational
    notes: List[str] = field(default_factory=list)
    # Set when the request is a diagnosis that may be followed by a repair.
    diagnostic: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "goal": self.goal,
            "strategy": self.strategy,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
        }


def _specialist_for(tool_name: Optional[str]) -> str:
    if not tool_name:
        return "linux"
    try:
        return registry.get(tool_name).category
    except Exception:  # noqa: BLE001
        return "linux"


def _steps(entries: List[Tuple[str, str, Dict[str, Any]]]) -> List[Step]:
    built: List[Step] = []
    for index, (description, tool_name, args) in enumerate(entries, start=1):
        built.append(
            Step(
                index=index,
                description=description,
                tool=tool_name,
                args=args,
                specialist=_specialist_for(tool_name),
            )
        )
    return built


# --------------------------------------------------------------------------
# Entity extraction
# --------------------------------------------------------------------------
SERVICE_WORDS = r"(?:service|daemon|unit|seva)"
POD_RE = re.compile(r"\bpod\s+([a-z0-9][a-z0-9.\-]{2,})", re.IGNORECASE)
NAMESPACE_RE = re.compile(r"\b(?:namespace|ns)\s+([a-z0-9][a-z0-9.\-]{1,})", re.IGNORECASE)
JOB_ID_RE = re.compile(r"\bjob\s*(?:id|#|number)?\s*(\d{1,7})\b", re.IGNORECASE)
QUOTED_RE = re.compile(r"[\"'`]([^\"'`]{2,64})[\"'`]")
SERVICE_RE = re.compile(
    r"\b(?:restart|reload|stop|start|status(?:\s+of)?|chalu|band|dobara)\s+(?:the\s+)?(?:%s\s+)?([a-z0-9][a-z0-9@._\-]{1,40})(?:\s+%s)?"
    % (SERVICE_WORDS, SERVICE_WORDS),
    re.IGNORECASE,
)
PACKAGE_RE = re.compile(r"\b(?:install|setup|lagao|install\s+karo)\s+(?:the\s+)?([a-z0-9][a-z0-9+.\-]{1,40})", re.IGNORECASE)

# Words that describe a unit but are never the unit's name.
NON_UNIT_WORDS = {
    "failed", "broken", "kharab", "crashed", "dead", "stopped", "running",
    "this", "that", "the", "a", "an", "my", "our", "it", "again", "back",
    "please", "immediately", "now", "down", "up", "sab", "koi",
}


def _usable_unit(token: str) -> bool:
    token = token.strip(".,").lower()
    return bool(token) and len(token) > 1 and token not in STOPWORDS and token not in NON_UNIT_WORDS


KNOWN_NAMESPACES = ("awx", "kube-system", "default")
STOPWORDS = {
    "the", "a", "an", "is", "me", "my", "this", "that", "it", "server", "system",
    "please", "karo", "karna", "kar", "do", "batao", "dikhao", "hai", "ka", "ki",
    "ke", "kya", "kitni", "kitna", "service", "services", "status", "and", "all",
}


def _namespace(text: str, default: str = "awx") -> str:
    match = NAMESPACE_RE.search(text)
    if match:
        return match.group(1).lower()
    for candidate in KNOWN_NAMESPACES:
        if re.search(r"\b%s\b" % re.escape(candidate), text, re.IGNORECASE):
            return candidate
    return default


# Friendly names users actually type, mapped to the real systemd units.
SERVICE_ALIASES = {
    "ai": "smart-awx-ai-agent",
    "ai agent": "smart-awx-ai-agent",
    "ai-agent": "smart-awx-ai-agent",
    "agent": "smart-awx-ai-agent",
    "ai service": "smart-awx-ai-agent",
    "ai backend": "smart-awx-ai-agent",
    "model": "ollama",
    "llm": "ollama",
    "ai runtime": "ollama",
    "kubernetes": "k3s",
    "k8s": "k3s",
}


def _service_name(text: str) -> Optional[str]:
    """Pull a systemd unit name out of free text, English or Hinglish."""
    lowered = text.lower()
    for phrase, unit in sorted(SERVICE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(phrase), lowered):
            if phrase in ("ai", "agent", "model", "llm") and not _has(lowered, r"service", r"agent", r"backend", r"runtime", r"daemon"):
                continue
            return unit

    quoted = QUOTED_RE.search(text)
    if quoted and " " not in quoted.group(1):
        return quoted.group(1)

    # "<name> service" / "service <name>"
    for pattern in (r"([a-z0-9][a-z0-9@._\-]{1,40})\s+(?:service|daemon|unit)", r"(?:service|daemon|unit)\s+([a-z0-9][a-z0-9@._\-]{1,40})"):
        match = re.search(pattern, lowered)
        if match and _usable_unit(match.group(1)):
            return match.group(1).strip(".,")

    # The first usable token after the verb ("restart nginx"), then before it
    # ("nginx restart karo" - Hinglish puts the object first).
    verb = re.search(r"(?<![a-z])(restart|reload|stop|start|chalu|band|dobara)(?![a-z])", lowered)
    if verb:
        for token in re.findall(r"[a-z0-9][a-z0-9@._\-]{1,40}", lowered[verb.end():]):
            if _usable_unit(token):
                return token.strip(".,")
        for token in reversed(re.findall(r"[a-z0-9][a-z0-9@._\-]{1,40}", lowered[: verb.start()])):
            if _usable_unit(token):
                return token.strip(".,")
    return None


def _pod_name(text: str) -> Optional[str]:
    match = POD_RE.search(text)
    if match:
        return match.group(1).strip(".,")
    quoted = QUOTED_RE.search(text)
    if quoted and re.fullmatch(r"[a-z0-9][a-z0-9.\-]{3,}", quoted.group(1)):
        return quoted.group(1)
    return None


def _job_id(text: str) -> Optional[int]:
    match = JOB_ID_RE.search(text)
    if match:
        return int(match.group(1))
    bare = re.search(r"#(\d{1,7})\b", text)
    return int(bare.group(1)) if bare else None


PACKAGE_REVERSE_RE = re.compile(r"\b([a-z0-9][a-z0-9+.\-]{1,40})\s+(?:ko\s+)?(?:install|setup|lagao)\b", re.IGNORECASE)


def _package(text: str) -> Optional[str]:
    """Handle both "install nginx" and the Hinglish "nginx install karo"."""
    for pattern in (PACKAGE_RE, PACKAGE_REVERSE_RE):
        match = pattern.search(text)
        if match:
            candidate = match.group(1).lower()
            if candidate not in STOPWORDS and not candidate.isdigit():
                return candidate
    return None


# --------------------------------------------------------------------------
# Deterministic router
# --------------------------------------------------------------------------
def _has(text: str, *words: str) -> bool:
    return any(re.search(r"(?<![a-z])%s" % w, text) for w in words)


def route(message: str) -> Optional[Plan]:
    """Match a request against known intents.  Returns None when unsure."""
    text = message.lower().strip()

    # ---- full health report -------------------------------------------
    if _has(text, r"health\s*report", r"complete\s+(?:health|status)", r"full\s+(?:health|status)", r"overall\s+(?:health|status)", r"poora\s+status", r"complete\s+server", r"sab\s+kuch\s+status") or (
        _has(text, r"health", r"swasth") and _has(text, r"server", r"system", r"stack", r"everything")
    ):
        return Plan(
            intent="health_report",
            goal="Produce a full stack health report from live data",
            steps=_steps([("Collect system, Kubernetes, AWX and AI runtime health", "get_health_report", {})]),
        )

    # ---- single resource questions -------------------------------------
    if _has(text, r"\bram\b", r"memory", r"mem\b", r"swap", r"memori"):
        if _has(text, r"pod", r"kubernetes", r"k8s", r"container"):
            pass  # fall through to the Kubernetes rules below
        else:
            return Plan(
                intent="memory",
                goal="Report real memory usage",
                steps=_steps([("Read memory usage from /proc/meminfo and free -h", "get_memory_info", {})]),
            )
    if _has(text, r"\bcpu\b", r"processor", r"core") and not _has(text, r"pod", r"kubernetes"):
        return Plan(
            intent="cpu",
            goal="Report real CPU model and utilisation",
            steps=_steps([("Sample CPU utilisation from /proc/stat", "get_cpu_info", {})]),
        )
    if _has(text, r"disk", r"storage", r"space", r"filesystem", r"\bdf\b"):
        return Plan(
            intent="disk",
            goal="Report real filesystem usage",
            steps=_steps([("Read filesystem usage with df", "get_disk_info", {})]),
        )
    if _has(text, r"uptime", r"kab\s+se", r"boot"):
        return Plan(intent="uptime", goal="Report uptime", steps=_steps([("Read /proc/uptime", "get_uptime", {})]))
    if _has(text, r"load\s*average", r"\bload\b"):
        return Plan(intent="load", goal="Report load average", steps=_steps([("Read /proc/loadavg", "get_load", {})]))
    if _has(text, r"process", r"top\s+(?:cpu|memory|consum)", r"konsa\s+process"):
        sort_by = "memory" if _has(text, r"memory", r"\bram\b") else "cpu"
        return Plan(
            intent="processes",
            goal="List the heaviest processes",
            steps=_steps([("List top processes by %s" % sort_by, "get_processes", {"sort_by": sort_by, "limit": 10})]),
        )
    if _has(text, r"\bports?\b", r"listening", r"socket"):
        return Plan(intent="ports", goal="List listening ports", steps=_steps([("List listening sockets", "get_ports", {})]))
    if _has(text, r"network", r"\bip\b", r"interface", r"gateway", r"\bdns\b") and not _has(text, r"kubernetes", r"pod"):
        return Plan(
            intent="network",
            goal="Report network configuration",
            steps=_steps([("Read interfaces, routes and resolvers", "get_network_info", {})]),
        )
    if _has(text, r"firewall", r"\bufw\b"):
        return Plan(intent="firewall", goal="Report firewall state", steps=_steps([("Read ufw status and local ports", "get_firewall_status", {})]))

    # ---- services -------------------------------------------------------
    # Restart is checked before "failed services" so "restart the failed
    # nginx service" is a repair request, not a listing request.
    if _has(text, r"restart", r"reboot\s+service", r"dobara\s+chalu") and not _has(text, r"\bpod\b", r"deployment", r"namespace"):
        service = _service_name(message)
        if service:
            return Plan(
                intent="restart_service",
                goal="Restart the %s service after checking its state" % service,
                steps=_steps(
                    [
                        ("Check the current state of %s" % service, "get_service_status", {"name": service}),
                        ("Restart %s" % service, "restart_service", {"name": service}),
                        ("Verify %s came back up" % service, "get_service_status", {"name": service}),
                    ]
                ),
                notes=["A restart is applied only if the permission policy allows it for this unit; otherwise you are asked first."],
            )
    if _has(text, r"failed\s+service", r"broken\s+service", r"kharab\s+service", r"failed\s+unit"):
        return Plan(
            intent="failed_services",
            goal="Find failed systemd services",
            steps=_steps([("List failed systemd units", "get_services", {"state": "failed"})]),
        )
    if _has(text, r"\bservices?\b", r"\bdaemon\b", r"\bunit\b"):
        service = _service_name(message)
        if service and _has(text, r"status", r"state", r"running", r"chal\s+rah"):
            return Plan(
                intent="service_status",
                goal="Report the state of %s" % service,
                steps=_steps([("Read systemd state for %s" % service, "get_service_status", {"name": service})]),
            )
        return Plan(
            intent="services",
            goal="List running services",
            steps=_steps([("List systemd services", "get_services", {"state": "running"})]),
        )

    # ---- logs -----------------------------------------------------------
    if _has(text, r"\blogs?\b", r"journal") and not _has(text, r"\bpod\b", r"awx\s+job"):
        return Plan(
            intent="logs",
            goal="Read recent system logs",
            steps=_steps([("Read the most recent journal entries", "get_logs", {"source": "system", "lines": 60})]),
        )

    # ---- packages -------------------------------------------------------
    if _has(text, r"install", r"lagao"):
        package = _package(text)
        if package:
            return Plan(
                intent="install_package",
                goal="Install %s, checking first whether it is already present" % package,
                steps=_steps(
                    [
                        ("Check whether %s is already installed" % package, "check_package", {"name": package}),
                        ("Install %s" % package, "install_package", {"name": package}),
                        ("Verify %s is installed" % package, "check_package", {"name": package}),
                    ]
                ),
                notes=["This is a MEDIUM RISK action and needs your approval before it runs."],
            )

    # ---- Kubernetes -----------------------------------------------------
    if _has(text, r"kubernetes", r"\bk8s\b", r"\bk3s\b", r"\bpods?\b", r"\bnodes?\b", r"cluster", r"deployment", r"namespace"):
        namespace = _namespace(text)
        pod = _pod_name(message)
        if _has(text, r"delete") and _has(text, r"namespace"):
            target = NAMESPACE_RE.search(text)
            return Plan(
                intent="delete_namespace",
                goal="Delete a Kubernetes namespace",
                steps=_steps(
                    [
                        ("List what currently exists in the namespace", "get_kubernetes_pods", {"namespace": target.group(1) if target else namespace}),
                        ("Delete the namespace", "delete_kubernetes_namespace", {"namespace": target.group(1) if target else namespace}),
                    ]
                ),
                notes=["Deleting a namespace is HIGH RISK: it destroys every workload, secret and PVC inside it."],
            )
        if pod and _has(text, r"why", r"fail", r"crash", r"error", r"restart.*loop", r"kyun", r"problem"):
            return Plan(
                intent="pod_diagnosis",
                goal="Find out why pod %s is failing" % pod,
                steps=_steps(
                    [
                        ("Describe pod %s" % pod, "describe_pod", {"pod": pod, "namespace": namespace}),
                        ("Read the current log of %s" % pod, "get_kubernetes_logs", {"pod": pod, "namespace": namespace, "lines": 80}),
                        ("Read the previous container log of %s" % pod, "get_kubernetes_logs", {"pod": pod, "namespace": namespace, "lines": 60, "previous": True}),
                        ("Check warning events in %s" % namespace, "get_kubernetes_events", {"namespace": namespace, "type": "Warning", "limit": 15}),
                        ("Check host memory headroom", "get_memory_info", {}),
                    ]
                ),
                diagnostic=True,
            )
        if pod and _has(text, r"restart"):
            return Plan(
                intent="restart_pod",
                goal="Restart pod %s" % pod,
                steps=_steps(
                    [
                        ("Check the current state of %s" % pod, "describe_pod", {"pod": pod, "namespace": namespace}),
                        ("Delete %s so its controller recreates it" % pod, "restart_pod", {"pod": pod, "namespace": namespace}),
                    ]
                ),
            )
        if pod and _has(text, r"\blogs?\b"):
            return Plan(
                intent="pod_logs",
                goal="Read logs of pod %s" % pod,
                steps=_steps([("Read logs of %s" % pod, "get_kubernetes_logs", {"pod": pod, "namespace": namespace, "lines": 80})]),
            )
        if _has(text, r"\bnodes?\b"):
            return Plan(intent="k8s_nodes", goal="Report cluster nodes", steps=_steps([("Query Kubernetes nodes", "get_kubernetes_nodes", {})]))
        if _has(text, r"event"):
            return Plan(
                intent="k8s_events",
                goal="Report recent cluster events",
                steps=_steps([("Query recent events", "get_kubernetes_events", {"namespace": namespace, "limit": 25})]),
            )
        if _has(text, r"deployment"):
            return Plan(
                intent="k8s_deployments",
                goal="Report deployments",
                steps=_steps([("Query deployments", "get_kubernetes_deployments", {"namespace": namespace})]),
            )
        if _has(text, r"service", r"\bsvc\b"):
            return Plan(
                intent="k8s_services",
                goal="Report services",
                steps=_steps([("Query services", "get_kubernetes_services", {"namespace": namespace})]),
            )
        if _has(text, r"health", r"healthy", r"theek"):
            return Plan(intent="k8s_health", goal="Report cluster health", steps=_steps([("Query cluster health", "get_kubernetes_health", {})]))
        all_ns = bool(_has(text, r"all\s+namespace", r"\bevery\b", r"cluster\s*wide", r"sab")) or not NAMESPACE_RE.search(text)
        return Plan(
            intent="k8s_pods",
            goal="Report Kubernetes pods",
            steps=_steps([("Query pods", "get_kubernetes_pods", {"all_namespaces": all_ns} if all_ns else {"namespace": namespace})]),
        )

    # ---- AWX ------------------------------------------------------------
    if _has(text, r"\bawx\b") or _has(text, r"job\s*template", r"ansible"):
        if _has(text, r"fail", r"error", r"kharab") and _has(text, r"job"):
            return Plan(
                intent="awx_failed_jobs",
                goal="Find failed AWX jobs and explain why they failed",
                steps=_steps([("Query the AWX API for failed jobs and their error output", "get_awx_failed_jobs", {"limit": 5})]),
                diagnostic=True,
            )
        if _has(text, r"\bdown\b", r"not\s+working", r"fix", r"theek", r"repair", r"unhealthy", r"broken"):
            return Plan(
                intent="awx_selfheal",
                goal="Diagnose why AWX is unhealthy before changing anything",
                steps=_steps(
                    [
                        ("Check the AWX API and authentication", "get_awx_health", {}),
                        ("Check AWX pods in the awx namespace", "get_kubernetes_pods", {"namespace": "awx"}),
                        ("Check recent warning events in the awx namespace", "get_kubernetes_events", {"namespace": "awx", "type": "Warning", "limit": 15}),
                        ("Check AWX deployments and their replica counts", "get_kubernetes_deployments", {"namespace": "awx"}),
                        ("Check host memory, CPU and disk headroom", "get_resource_summary", {}),
                    ]
                ),
                diagnostic=True,
                notes=["Diagnosis first. Any repair is proposed with its risk level and needs approval."],
            )
        if _has(text, r"project"):
            return Plan(intent="awx_projects", goal="List AWX projects", steps=_steps([("Query AWX projects", "list_awx_projects", {})]))
        if _has(text, r"inventor"):
            return Plan(intent="awx_inventories", goal="List AWX inventories", steps=_steps([("Query AWX inventories", "list_awx_inventories", {})]))
        if _has(text, r"template"):
            if _has(text, r"launch", r"\brun\b", r"execute", r"chalao"):
                quoted = QUOTED_RE.search(message)
                args: Dict[str, Any] = {}
                if quoted:
                    args["template_name"] = quoted.group(1)
                return Plan(
                    intent="awx_launch",
                    goal="Launch an AWX job template",
                    steps=_steps(
                        [
                            ("List matching job templates", "list_awx_job_templates", {}),
                            ("Launch the job template through the AWX API", "launch_job_template", args),
                        ]
                    ),
                    notes=["Launching a job template is MEDIUM RISK and needs your approval."],
                )
            return Plan(intent="awx_templates", goal="List AWX job templates", steps=_steps([("Query AWX job templates", "list_awx_job_templates", {})]))
        job_id = _job_id(text)
        if job_id and _has(text, r"output", r"log", r"stdout"):
            return Plan(
                intent="awx_job_output",
                goal="Read the output of AWX job %d" % job_id,
                steps=_steps([("Read job %d output" % job_id, "get_awx_job_output", {"job_id": job_id})]),
            )
        if job_id:
            return Plan(
                intent="awx_job",
                goal="Report AWX job %d" % job_id,
                steps=_steps([("Read job %d" % job_id, "get_awx_job", {"job_id": job_id})]),
            )
        if _has(text, r"\bjobs?\b"):
            return Plan(intent="awx_jobs", goal="List recent AWX jobs", steps=_steps([("Query recent AWX jobs", "list_awx_jobs", {"limit": 20})]))
        return Plan(
            intent="awx_status",
            goal="Report AWX health",
            steps=_steps([("Query the AWX API ping and authentication", "get_awx_health", {})]),
        )

    # ---- optimisation / open ended inspection ---------------------------
    if _has(text, r"optimi[sz]e", r"tune", r"slow", r"performance", r"dheema", r"improve"):
        return Plan(
            intent="optimize",
            goal="Inspect the machine before proposing any change",
            steps=_steps(
                [
                    ("Measure CPU, memory, disk and load", "get_resource_summary", {}),
                    ("Find the heaviest processes", "get_top_consumers", {"limit": 5}),
                    ("Check for failed services", "get_services", {"state": "failed"}),
                    ("Check Kubernetes workload health", "get_kubernetes_health", {}),
                    ("Check pods that keep restarting", "get_pod_restart_summary", {}),
                    ("Check AWX health", "get_awx_health", {}),
                ]
            ),
            diagnostic=True,
            notes=["Nothing is changed by this plan. You get findings, risks and recommended actions first."],
        )

    # ---- AI runtime -----------------------------------------------------
    if _has(text, r"\bmodel\b", r"\bollama\b", r"\bllm\b", r"ai\s+runtime", r"ai\s+service"):
        return Plan(
            intent="ai_runtime",
            goal="Report the local AI runtime state",
            steps=_steps([("Query the Ollama runtime", "get_ai_runtime_status", {})]),
        )

    # ---- generic "status" ------------------------------------------------
    if _has(text, r"\bstatus\b", r"\bsummary\b", r"overview", r"haal", r"kaisa\s+hai"):
        return Plan(
            intent="system_status",
            goal="Report overall system status",
            steps=_steps([("Collect OS, CPU, memory, disk and load", "get_system_info", {})]),
        )

    return None


# --------------------------------------------------------------------------
# LLM tool selection
# --------------------------------------------------------------------------
PLANNER_SYSTEM = """You are the planning component of a server operations agent.
You do NOT answer questions. You only choose which tools to run.

Rules:
- You may only use tools from the provided catalogue. Never invent a tool name.
- Facts about the machine (RAM, CPU, disk, pods, jobs, logs) must always come
  from a tool. Never answer from memory.
- Prefer read-only tools. Only choose a write tool when the user clearly asked
  for a change.
- Choose the fewest tools that fully answer the request (1-4 is typical).

Reply with JSON only, in exactly this shape:
{"intent": "<short_snake_case_intent>",
 "goal": "<one sentence>",
 "steps": [{"description": "<what this step establishes>",
            "tool": "<tool name from the catalogue>",
            "args": {}}]}"""


def _catalog_digest(max_tools: int = 60) -> str:
    rows = []
    for item in registry.catalog()[:max_tools]:
        properties = (item.get("parameters") or {}).get("properties") or {}
        params = ", ".join(sorted(properties)) or "none"
        rows.append("- %s [%s, %s] %s (args: %s)" % (item["name"], item["category"], item["permission"], item["description"], params))
    return "\n".join(rows)


def plan_with_llm(message: str, history: Optional[List[Dict[str, str]]] = None) -> Optional[Plan]:
    """Ask the local model to choose tools.  Returns None if it cannot."""
    messages = [{"role": "system", "content": PLANNER_SYSTEM + "\n\nTOOL CATALOGUE:\n" + _catalog_digest()}]
    for turn in (history or [])[-4:]:
        if turn.get("role") in ("user", "assistant"):
            messages.append({"role": turn["role"], "content": turn["content"][:800]})
    messages.append({"role": "user", "content": message[:2000]})

    try:
        reply = llm_client().chat(messages, temperature=0.0, json_mode=True, max_tokens=500, timeout=90)
    except LLMUnavailable:
        return None

    payload = extract_json(reply.text)
    if not isinstance(payload, dict):
        return None

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    entries: List[Tuple[str, str, Dict[str, Any]]] = []
    dropped: List[str] = []
    known = registry.all_tools()
    for raw in raw_steps[:6]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("tool") or "").strip()
        if name not in known:
            dropped.append(name or "(empty)")
            continue
        args = raw.get("args")
        entries.append((str(raw.get("description") or "Run %s" % name)[:200], name, args if isinstance(args, dict) else {}))

    if not entries:
        return None

    plan = Plan(
        intent=str(payload.get("intent") or "llm_selected")[:40],
        goal=str(payload.get("goal") or "Answer the request using live data")[:200],
        steps=_steps(entries),
        strategy="llm",
    )
    if dropped:
        plan.notes.append("Ignored %d tool name(s) the model invented: %s" % (len(dropped), ", ".join(dropped[:3])))
    return plan


def fallback_plan(message: str) -> Plan:
    """Last resort: inspect broadly and be explicit that this is a fallback."""
    return Plan(
        intent="fallback_inspection",
        goal="Collect general system state because the request could not be mapped to a specific tool",
        steps=_steps(
            [
                ("Collect OS, CPU, memory, disk and load", "get_system_info", {}),
                ("Check Kubernetes and AWX health", "get_health_report", {}),
            ]
        ),
        strategy="fallback",
        notes=[
            "I could not map that request to a specific tool, so I collected general system state instead. "
            "Ask about CPU, RAM, disk, services, logs, Kubernetes pods/nodes/events or AWX jobs for a precise answer."
        ],
    )


def build(message: str, history: Optional[List[Dict[str, str]]] = None, allow_llm: bool = True) -> Plan:
    """Full planning pipeline: rules first, then the model, then the fallback."""
    plan = route(message)
    if plan is not None:
        return plan
    if allow_llm:
        plan = plan_with_llm(message, history)
        if plan is not None:
            return plan
    return fallback_plan(message)
