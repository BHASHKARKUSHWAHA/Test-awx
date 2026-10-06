"""AWX tools (spec sections 18 and 20).

A thin, dependency-free AWX API client plus the tools that expose it to the
agent.  Credentials come from the environment (populated from a Kubernetes
secret / root-only env file) and are never hardcoded or echoed back.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .registry import Level, ToolError, tool

AWX_NAME_RE = r"[A-Za-z0-9 _.\-]{1,128}"


class AWXError(ToolError):
    pass


class AWXClient:
    """Minimal AWX REST client (token or basic auth, no third-party deps)."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        verify_tls: Optional[bool] = None,
        timeout: int = 20,
    ) -> None:
        # The agent runs on the host, so the NodePort is the right default;
        # install.py writes the real value into /etc/smart-awx-ai/agent.env.
        self.base_url = (base_url or os.environ.get("AWX_URL") or "http://127.0.0.1:30081").rstrip("/")
        self.username = username or os.environ.get("AWX_USERNAME") or "admin"
        self.password = password if password is not None else os.environ.get("AWX_PASSWORD")
        self.token = token or os.environ.get("AWX_TOKEN")
        env_verify = os.environ.get("AWX_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
        self.verify_tls = env_verify if verify_tls is None else verify_tls
        self.timeout = timeout

    # -- plumbing -------------------------------------------------------
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "smart-awx-ai-agent/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        elif self.password:
            raw = ("%s:%s" % (self.username, self.password)).encode("utf-8")
            headers["Authorization"] = "Basic %s" % base64.b64encode(raw).decode("ascii")
        if extra:
            headers.update(extra)
        return headers

    def _context(self) -> Optional[ssl.SSLContext]:
        if not self.base_url.startswith("https"):
            return None
        if self.verify_tls:
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        url = self.base_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context()) as response:
                payload = response.read().decode("utf-8", "replace")
                try:
                    return response.status, json.loads(payload) if payload else None
                except ValueError:
                    return response.status, payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (401, 403):
                raise AWXError("AWX rejected the agent credentials (HTTP %d). Check /etc/smart-awx-ai/agent.env." % exc.code)
            raise AWXError("AWX API %s %s failed with HTTP %d: %s" % (method, path, exc.code, detail))
        except urllib.error.URLError as exc:
            raise AWXError("cannot reach AWX at %s: %s" % (self.base_url, exc.reason))
        except TimeoutError:
            raise AWXError("AWX API timed out after %ss (%s)" % (self.timeout, path))

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)[1]

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("POST", path, body=body)[1]

    def list(self, path: str, params: Optional[Dict[str, Any]] = None, max_items: int = 200) -> List[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("page_size", min(max_items, 100))
        results: List[Dict[str, Any]] = []
        next_path: Optional[str] = None
        payload = self.get(path, params)
        while True:
            if not isinstance(payload, dict):
                break
            results.extend(payload.get("results") or [])
            next_path = payload.get("next")
            if not next_path or len(results) >= max_items:
                break
            payload = self.get(next_path)
        return results[:max_items]

    # -- convenience ----------------------------------------------------
    def ping(self) -> Dict[str, Any]:
        return self.get("/api/v2/ping/")

    def me(self) -> Dict[str, Any]:
        return self.get("/api/v2/me/")


_client: Optional[AWXClient] = None


def client() -> AWXClient:
    global _client
    if _client is None:
        _client = AWXClient()
    return _client


def reset_client() -> None:
    global _client
    _client = None


def _evidence(method: str, path: str, note: str = "") -> Dict[str, Any]:
    return {
        "command": "AWX API %s %s" % (method, path),
        "exit_code": 0,
        "stdout": note[:2000],
        "stderr": "",
    }


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------
@tool(
    name="get_awx_health",
    description="AWX health: API ping, version, instance/capacity state and whether the agent credentials work.",
    category="awx",
    level=Level.READ_ONLY,
    timeout=40,
)
def get_awx_health() -> Tuple[Dict[str, Any], List[Any]]:
    awx = client()
    ping = awx.ping()
    instances = ping.get("instances") or []
    authenticated = False
    username = None
    try:
        me = awx.me()
        results = me.get("results") or []
        authenticated = bool(results)
        username = results[0].get("username") if results else None
    except AWXError as exc:
        return (
            {
                "reachable": True,
                "version": ping.get("version"),
                "authenticated": False,
                "auth_error": str(exc),
                "status": "degraded",
            },
            [_evidence("GET", "/api/v2/ping/", json.dumps(ping)[:1000])],
        )
    healthy_nodes = [i for i in instances if i.get("capacity", 0) > 0]
    return (
        {
            "reachable": True,
            "version": ping.get("version"),
            "ha": ping.get("ha"),
            "instance_count": len(instances),
            "instances_with_capacity": len(healthy_nodes),
            "authenticated": authenticated,
            "authenticated_as": username,
            "status": "healthy" if (authenticated and healthy_nodes) else "degraded",
            "base_url": awx.base_url,
        },
        [_evidence("GET", "/api/v2/ping/", json.dumps(ping)[:1500]), _evidence("GET", "/api/v2/me/", "authenticated as %s" % username)],
    )


@tool(
    name="list_awx_projects",
    description="AWX projects with their SCM type, URL and last update status.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}}, "required": []},
    timeout=40,
)
def list_awx_projects(limit: int = 50) -> Tuple[Dict[str, Any], List[Any]]:
    rows = client().list("/api/v2/projects/", max_items=limit)
    projects = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "scm_type": r.get("scm_type") or "manual",
            "scm_url": r.get("scm_url"),
            "status": r.get("status"),
            "last_updated": r.get("last_updated"),
        }
        for r in rows
    ]
    return (
        {"count": len(projects), "failed": [p for p in projects if p["status"] in ("failed", "error")], "projects": projects},
        [_evidence("GET", "/api/v2/projects/", "%d projects" % len(projects))],
    )


@tool(
    name="list_awx_inventories",
    description="AWX inventories with host and group counts.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}}, "required": []},
    timeout=40,
)
def list_awx_inventories(limit: int = 50) -> Tuple[Dict[str, Any], List[Any]]:
    rows = client().list("/api/v2/inventories/", max_items=limit)
    inventories = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "description": r.get("description"),
            "total_hosts": r.get("total_hosts"),
            "total_groups": r.get("total_groups"),
            "hosts_with_active_failures": r.get("hosts_with_active_failures"),
        }
        for r in rows
    ]
    return ({"count": len(inventories), "inventories": inventories}, [_evidence("GET", "/api/v2/inventories/", "%d inventories" % len(inventories))])


@tool(
    name="list_awx_job_templates",
    description="AWX job templates that can be launched, with their project, inventory and playbook.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}, "search": {"type": "string", "pattern": AWX_NAME_RE, "maxLength": 128}}, "required": []},
    timeout=40,
)
def list_awx_job_templates(limit: int = 50, search: Optional[str] = None) -> Tuple[Dict[str, Any], List[Any]]:
    params = {"search": search} if search else None
    rows = client().list("/api/v2/job_templates/", params=params, max_items=limit)
    templates = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "playbook": r.get("playbook"),
            "project": (r.get("summary_fields") or {}).get("project", {}).get("name"),
            "inventory": (r.get("summary_fields") or {}).get("inventory", {}).get("name"),
            "ask_variables_on_launch": r.get("ask_variables_on_launch"),
            "last_job_status": (r.get("summary_fields") or {}).get("last_job", {}).get("status"),
        }
        for r in rows
    ]
    return ({"count": len(templates), "job_templates": templates}, [_evidence("GET", "/api/v2/job_templates/", "%d templates" % len(templates))])


def _job_row(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "status": r.get("status"),
        "failed": r.get("failed"),
        "started": r.get("started"),
        "finished": r.get("finished"),
        "elapsed_seconds": r.get("elapsed"),
        "job_template_id": (r.get("summary_fields") or {}).get("job_template", {}).get("id"),
        "launched_by": (r.get("summary_fields") or {}).get("created_by", {}).get("username"),
    }


@tool(
    name="list_awx_jobs",
    description="Recent AWX jobs. status=failed answers 'show me failed jobs'.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["all", "successful", "failed", "error", "canceled", "running", "pending"], "default": "all"},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
    timeout=40,
)
def list_awx_jobs(status: str = "all", limit: int = 20) -> Tuple[Dict[str, Any], List[Any]]:
    params: Dict[str, Any] = {"order_by": "-id"}
    if status != "all":
        params["status"] = status
    rows = client().list("/api/v2/jobs/", params=params, max_items=limit)
    jobs = [_job_row(r) for r in rows]
    return (
        {
            "filter": status,
            "count": len(jobs),
            "failed_count": len([j for j in jobs if j["status"] in ("failed", "error")]),
            "jobs": jobs,
        },
        [_evidence("GET", "/api/v2/jobs/?status=%s" % status, "%d jobs" % len(jobs))],
    )


@tool(
    name="get_awx_failed_jobs",
    description="AWX jobs that failed or errored, newest first, with the first error lines from each job's output.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20}}, "required": []},
    timeout=60,
)
def get_awx_failed_jobs(limit: int = 5) -> Tuple[Dict[str, Any], List[Any]]:
    awx = client()
    rows = awx.list("/api/v2/jobs/", params={"order_by": "-id", "failed": "true"}, max_items=limit)
    jobs = []
    evidence = [_evidence("GET", "/api/v2/jobs/?failed=true", "%d failed jobs" % len(rows))]
    for row in rows:
        job = _job_row(row)
        try:
            stdout = awx.request("GET", "/api/v2/jobs/%s/stdout/" % job["id"], params={"format": "txt"})[1] or ""
        except AWXError as exc:
            stdout = "(stdout unavailable: %s)" % exc
        text = stdout if isinstance(stdout, str) else json.dumps(stdout)
        error_lines = [line for line in text.splitlines() if any(k in line.lower() for k in ("fatal:", "error", "failed=", "unreachable"))]
        job["error_excerpt"] = "\n".join(error_lines[-12:])[:1500]
        jobs.append(job)
        evidence.append(_evidence("GET", "/api/v2/jobs/%s/stdout/" % job["id"], job["error_excerpt"]))
    return ({"count": len(jobs), "failed_jobs": jobs}, evidence)


@tool(
    name="get_awx_job",
    description="Status and metadata of one AWX job by id.",
    category="awx",
    level=Level.READ_ONLY,
    parameters={"type": "object", "properties": {"job_id": {"type": "integer", "minimum": 1}}, "required": ["job_id"]},
    timeout=40,
)
def get_awx_job(job_id: int) -> Tuple[Dict[str, Any], List[Any]]:
    row = client().get("/api/v2/jobs/%d/" % job_id)
    data = _job_row(row)
    data.update(
        {
            "playbook": row.get("playbook"),
            "job_explanation": row.get("job_explanation"),
            "result_traceback": (row.get("result_traceback") or "")[:1500],
            "extra_vars_present": bool(row.get("extra_vars") and row["extra_vars"] not in ("{}", "")),
        }
    )
    return (data, [_evidence("GET", "/api/v2/jobs/%d/" % job_id, json.dumps(data)[:1000])])


@tool(
    name="get_awx_job_output",
    description="Plain-text stdout of an AWX job (tail).",
    category="awx",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "integer", "minimum": 1}, "lines": {"type": "integer", "default": 80, "minimum": 1, "maximum": 1000}},
        "required": ["job_id"],
    },
    timeout=60,
)
def get_awx_job_output(job_id: int, lines: int = 80) -> Tuple[Dict[str, Any], List[Any]]:
    payload = client().request("GET", "/api/v2/jobs/%d/stdout/" % job_id, params={"format": "txt"})[1] or ""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    tail = "\n".join(text.splitlines()[-lines:])
    return (
        {"job_id": job_id, "lines": len(tail.splitlines()), "output": tail[-8000:]},
        [_evidence("GET", "/api/v2/jobs/%d/stdout/" % job_id, "%d lines" % len(tail.splitlines()))],
    )


# --------------------------------------------------------------------------
# Write-level tools
# --------------------------------------------------------------------------
@tool(
    name="launch_job_template",
    description="Launch an AWX job template by id or exact name. This is the AWX-first way to change the managed estate.",
    category="awx",
    level=Level.MEDIUM_RISK,
    parameters={
        "type": "object",
        "properties": {
            "template_id": {"type": "integer", "minimum": 1},
            "template_name": {"type": "string", "pattern": AWX_NAME_RE, "maxLength": 128},
            "wait": {"type": "boolean", "default": True},
            "timeout_seconds": {"type": "integer", "default": 300, "minimum": 10, "maximum": 1800},
        },
        "required": [],
    },
    timeout=1800,
    risk_note="Runs an Ansible playbook against the inventory configured on the template.",
)
def launch_job_template(template_id: Optional[int] = None, template_name: Optional[str] = None, wait: bool = True, timeout_seconds: int = 300) -> Tuple[Dict[str, Any], List[Any]]:
    awx = client()
    if not template_id and not template_name:
        raise ToolError("provide template_id or template_name")
    if not template_id:
        matches = awx.list("/api/v2/job_templates/", params={"name": template_name}, max_items=5)
        if not matches:
            raise ToolError("no AWX job template named %r" % template_name)
        if len(matches) > 1:
            raise ToolError("job template name %r is ambiguous (%d matches); pass template_id" % (template_name, len(matches)))
        template_id = matches[0]["id"]

    launched = awx.post("/api/v2/job_templates/%d/launch/" % template_id, body={})
    job_id = launched.get("id") if isinstance(launched, dict) else None
    if not job_id:
        raise ToolError("AWX did not return a job id for the launch: %s" % json.dumps(launched)[:300])

    evidence = [_evidence("POST", "/api/v2/job_templates/%d/launch/" % template_id, "job %s created" % job_id)]
    result: Dict[str, Any] = {"template_id": template_id, "job_id": job_id, "waited": wait}
    if wait:
        deadline = time.time() + timeout_seconds
        status = "pending"
        while time.time() < deadline:
            time.sleep(5)
            row = awx.get("/api/v2/jobs/%d/" % job_id)
            status = row.get("status")
            if status in ("successful", "failed", "error", "canceled"):
                break
        result["final_status"] = status
        result["succeeded"] = status == "successful"
        if status in ("failed", "error"):
            output, _ = get_awx_job_output(job_id, 40)
            result["error_output"] = output["output"][-2000:]
        evidence.append(_evidence("GET", "/api/v2/jobs/%d/" % job_id, "final status %s" % status))
    return (result, evidence)


@tool(
    name="relaunch_job",
    description="Re-run an existing AWX job with the same parameters. Safe retry for a transient failure.",
    category="awx",
    level=Level.SAFE_WRITE,
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "integer", "minimum": 1},
            "wait": {"type": "boolean", "default": True},
            "timeout_seconds": {"type": "integer", "default": 300, "minimum": 10, "maximum": 1800},
        },
        "required": ["job_id"],
    },
    timeout=1800,
    risk_note="Runs the same playbook again against the same inventory.",
)
def relaunch_job(job_id: int, wait: bool = True, timeout_seconds: int = 300) -> Tuple[Dict[str, Any], List[Any]]:
    awx = client()
    relaunched = awx.post("/api/v2/jobs/%d/relaunch/" % job_id, body={})
    new_id = relaunched.get("id") if isinstance(relaunched, dict) else None
    if not new_id:
        raise ToolError("AWX refused the relaunch: %s" % json.dumps(relaunched)[:300])
    evidence = [_evidence("POST", "/api/v2/jobs/%d/relaunch/" % job_id, "new job %s" % new_id)]
    result = {"original_job_id": job_id, "new_job_id": new_id, "waited": wait}
    if wait:
        deadline = time.time() + timeout_seconds
        status = "pending"
        while time.time() < deadline:
            time.sleep(5)
            status = awx.get("/api/v2/jobs/%d/" % new_id).get("status")
            if status in ("successful", "failed", "error", "canceled"):
                break
        result["final_status"] = status
        result["succeeded"] = status == "successful"
        evidence.append(_evidence("GET", "/api/v2/jobs/%d/" % new_id, "final status %s" % status))
    return (result, evidence)
