"""Kubernetes tools (spec section 17).

Everything goes through ``kubectl`` against the K3s kubeconfig; nothing is
cached and nothing is inferred.  If kubectl is missing or the API server is
down, the tool says so instead of returning an empty, reassuring answer.
"""

from __future__ import annotations

import json
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from .registry import Exec, Level, ToolError, sh, tool

K8S_NAME = r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?"
DEFAULT_NAMESPACE = "awx"


def _kubectl(args: List[str], timeout: int = 45) -> Exec:
    if shutil.which("kubectl"):
        return sh(["kubectl"] + args, timeout=timeout)
    if shutil.which("k3s"):
        return sh(["k3s", "kubectl"] + args, timeout=timeout)
    raise ToolError("kubectl is not installed on this machine")


def _kubectl_json(args: List[str], timeout: int = 45) -> Tuple[Any, Exec]:
    res = _kubectl(args + ["-o", "json"], timeout=timeout)
    if not res.ok:
        return None, res
    try:
        return json.loads(res.stdout), res
    except ValueError:
        return None, res


def _require(data: Any, res: Exec, what: str) -> Any:
    if data is None:
        raise ToolError("could not read %s from Kubernetes: %s" % (what, (res.stderr or res.stdout).strip()[:300]))
    return data


def _container_states(pod: Dict[str, Any]) -> Tuple[List[str], int]:
    reasons: List[str] = []
    restarts = 0
    for status in pod.get("status", {}).get("containerStatuses") or []:
        restarts += status.get("restartCount", 0)
        state = status.get("state", {})
        if "waiting" in state and state["waiting"].get("reason"):
            reasons.append(state["waiting"]["reason"])
        elif "terminated" in state and state["terminated"].get("reason"):
            reasons.append(state["terminated"]["reason"])
    return reasons, restarts


def _summarise_pod(pod: Dict[str, Any]) -> Dict[str, Any]:
    meta = pod.get("metadata", {})
    status = pod.get("status", {})
    reasons, restarts = _container_states(pod)
    ready_containers = sum(1 for cs in status.get("containerStatuses") or [] if cs.get("ready"))
    total_containers = len(pod.get("spec", {}).get("containers") or [])
    conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "phase": status.get("phase"),
        "ready": "%d/%d" % (ready_containers, total_containers),
        "is_ready": conditions.get("Ready") == "True",
        "restarts": restarts,
        "node": pod.get("spec", {}).get("nodeName"),
        "ip": status.get("podIP"),
        "start_time": status.get("startTime"),
        "problem_reasons": reasons,
        "healthy": conditions.get("Ready") == "True" and status.get("phase") in ("Running", "Succeeded"),
    }


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------
@tool(
    name="get_kubernetes_nodes",
    description="Cluster nodes with Ready condition, roles, kubelet version and allocatable capacity.",
    category="kubernetes",
    level=Level.READ_ONLY,
)
def get_kubernetes_nodes() -> Tuple[Dict[str, Any], List[Any]]:
    data, res = _kubectl_json(["get", "nodes"])
    _require(data, res, "nodes")
    nodes = []
    for item in data.get("items", []):
        conditions = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
        info = item.get("status", {}).get("nodeInfo", {})
        allocatable = item.get("status", {}).get("allocatable", {})
        nodes.append(
            {
                "name": item["metadata"]["name"],
                "ready": conditions.get("Ready") == "True",
                "memory_pressure": conditions.get("MemoryPressure") == "True",
                "disk_pressure": conditions.get("DiskPressure") == "True",
                "kubelet_version": info.get("kubeletVersion"),
                "os_image": info.get("osImage"),
                "container_runtime": info.get("containerRuntimeVersion"),
                "allocatable_cpu": allocatable.get("cpu"),
                "allocatable_memory": allocatable.get("memory"),
                "allocatable_pods": allocatable.get("pods"),
            }
        )
    return ({"count": len(nodes), "all_ready": all(n["ready"] for n in nodes) if nodes else False, "nodes": nodes}, [res])


@tool(
    name="get_kubernetes_pods",
    description="Pods in a namespace (or all namespaces) with phase, readiness, restarts and failure reasons.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "all_namespaces": {"type": "boolean", "default": False},
            "only_problems": {"type": "boolean", "default": False},
        },
        "required": [],
    },
)
def get_kubernetes_pods(namespace: Optional[str] = None, all_namespaces: bool = False, only_problems: bool = False) -> Tuple[Dict[str, Any], List[Any]]:
    args = ["get", "pods"]
    if all_namespaces:
        args.append("--all-namespaces")
    else:
        args.extend(["-n", namespace or DEFAULT_NAMESPACE])
    data, res = _kubectl_json(args)
    _require(data, res, "pods")
    pods = [_summarise_pod(item) for item in data.get("items", [])]
    unhealthy = [p for p in pods if not p["healthy"]]
    return (
        {
            "scope": "all namespaces" if all_namespaces else (namespace or DEFAULT_NAMESPACE),
            "count": len(pods),
            "healthy_count": len(pods) - len(unhealthy),
            "unhealthy_count": len(unhealthy),
            "unhealthy": unhealthy,
            "pods": unhealthy if only_problems else pods,
        },
        [res],
    )


@tool(
    name="get_kubernetes_services",
    description="Services in a namespace with type, cluster IP, ports and NodePort assignments.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "all_namespaces": {"type": "boolean", "default": False},
        },
        "required": [],
    },
)
def get_kubernetes_services(namespace: str = DEFAULT_NAMESPACE, all_namespaces: bool = False) -> Tuple[Dict[str, Any], List[Any]]:
    args = ["get", "svc"] + (["--all-namespaces"] if all_namespaces else ["-n", namespace])
    data, res = _kubectl_json(args)
    _require(data, res, "services")
    services = []
    for item in data.get("items", []):
        spec = item.get("spec", {})
        services.append(
            {
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "type": spec.get("type"),
                "cluster_ip": spec.get("clusterIP"),
                "ports": [
                    {"port": p.get("port"), "target_port": p.get("targetPort"), "node_port": p.get("nodePort"), "protocol": p.get("protocol")}
                    for p in spec.get("ports", [])
                ],
            }
        )
    return ({"count": len(services), "services": services}, [res])


@tool(
    name="get_kubernetes_deployments",
    description="Deployments with desired/ready replica counts and their container images.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "all_namespaces": {"type": "boolean", "default": False},
        },
        "required": [],
    },
)
def get_kubernetes_deployments(namespace: str = DEFAULT_NAMESPACE, all_namespaces: bool = False) -> Tuple[Dict[str, Any], List[Any]]:
    args = ["get", "deployments"] + (["--all-namespaces"] if all_namespaces else ["-n", namespace])
    data, res = _kubectl_json(args)
    _require(data, res, "deployments")
    deployments = []
    for item in data.get("items", []):
        status = item.get("status", {})
        containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        deployments.append(
            {
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "desired": item.get("spec", {}).get("replicas", 0),
                "ready": status.get("readyReplicas", 0),
                "available": status.get("availableReplicas", 0),
                "updated": status.get("updatedReplicas", 0),
                "healthy": (status.get("readyReplicas", 0) or 0) >= (item.get("spec", {}).get("replicas", 0) or 0),
                "images": [c.get("image") for c in containers],
                "resources": {c.get("name"): c.get("resources", {}) for c in containers},
            }
        )
    return (
        {"count": len(deployments), "unhealthy": [d for d in deployments if not d["healthy"]], "deployments": deployments},
        [res],
    )


@tool(
    name="get_kubernetes_events",
    description="Recent cluster events, newest last. Use type=Warning to see what is going wrong.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "all_namespaces": {"type": "boolean", "default": False},
            "type": {"type": "string", "enum": ["All", "Warning", "Normal"], "default": "All"},
            "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
)
def get_kubernetes_events(namespace: str = DEFAULT_NAMESPACE, all_namespaces: bool = False, type: str = "All", limit: int = 25) -> Tuple[Dict[str, Any], List[Any]]:
    args = ["get", "events"] + (["--all-namespaces"] if all_namespaces else ["-n", namespace])
    if type != "All":
        args += ["--field-selector", "type=%s" % type]
    data, res = _kubectl_json(args)
    _require(data, res, "events")
    events = []
    for item in data.get("items", []):
        involved = item.get("involvedObject", {})
        events.append(
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "object": "%s/%s" % (involved.get("kind"), involved.get("name")),
                "namespace": item["metadata"].get("namespace"),
                "message": (item.get("message") or "")[:300],
                "count": item.get("count"),
                "last_seen": item.get("lastTimestamp") or item.get("eventTime"),
            }
        )
    events.sort(key=lambda e: e.get("last_seen") or "")
    trimmed = events[-limit:]
    return ({"count": len(trimmed), "warnings": [e for e in trimmed if e["type"] == "Warning"], "events": trimmed}, [res])


@tool(
    name="get_kubernetes_logs",
    description="Logs from a pod. Use previous=true to read the log of a crashed container.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "pod": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "container": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "lines": {"type": "integer", "default": 60, "minimum": 1, "maximum": 500},
            "previous": {"type": "boolean", "default": False},
        },
        "required": ["pod"],
    },
    timeout=60,
)
def get_kubernetes_logs(pod: str, namespace: str = DEFAULT_NAMESPACE, container: Optional[str] = None, lines: int = 60, previous: bool = False) -> Tuple[Dict[str, Any], List[Any]]:
    args = ["logs", "-n", namespace, pod, "--tail", str(lines)]
    if container:
        args += ["-c", container]
    else:
        args.append("--all-containers")
    if previous:
        args.append("--previous")
    res = _kubectl(args, timeout=60)
    if not res.ok and previous:
        raise ToolError("no previous log for %s/%s: %s" % (namespace, pod, res.stderr.strip()[:200]))
    if not res.ok:
        raise ToolError("could not read logs for %s/%s: %s" % (namespace, pod, res.stderr.strip()[:200]))
    return ({"pod": pod, "namespace": namespace, "previous": previous, "lines": len(res.out.splitlines()), "log": res.out[-8000:]}, [res])


@tool(
    name="get_kubernetes_health",
    description="Overall cluster health: API readiness, node conditions, unhealthy pods and recent warnings.",
    category="kubernetes",
    level=Level.READ_ONLY,
    timeout=60,
)
def get_kubernetes_health() -> Tuple[Dict[str, Any], List[Any]]:
    readyz = _kubectl(["get", "--raw", "/readyz"], timeout=30)
    nodes_data, nodes_ev = get_kubernetes_nodes()
    pods_data, pods_ev = get_kubernetes_pods(all_namespaces=True)
    events_data, events_ev = get_kubernetes_events(all_namespaces=True, type="Warning", limit=10)
    healthy = readyz.ok and nodes_data["all_ready"] and pods_data["unhealthy_count"] == 0
    return (
        {
            "api_ready": readyz.ok,
            "api_response": readyz.out[:100],
            "nodes_ready": nodes_data["all_ready"],
            "node_count": nodes_data["count"],
            "pod_count": pods_data["count"],
            "unhealthy_pods": pods_data["unhealthy"],
            "unhealthy_pod_count": pods_data["unhealthy_count"],
            "recent_warnings": events_data["warnings"],
            "status": "healthy" if healthy else "degraded",
        },
        [readyz] + nodes_ev + pods_ev + events_ev,
    )


@tool(
    name="describe_pod",
    description="Full describe output for one pod: events, container states, resource limits and probe results.",
    category="kubernetes",
    level=Level.READ_ONLY,
    parameters={
        "type": "object",
        "properties": {
            "pod": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
        },
        "required": ["pod"],
    },
    timeout=60,
)
def describe_pod(pod: str, namespace: str = DEFAULT_NAMESPACE) -> Tuple[Dict[str, Any], List[Any]]:
    res = _kubectl(["describe", "pod", "-n", namespace, pod], timeout=60)
    if not res.ok:
        raise ToolError("could not describe %s/%s: %s" % (namespace, pod, res.stderr.strip()[:200]))
    data, json_res = _kubectl_json(["get", "pod", "-n", namespace, pod])
    summary = _summarise_pod(data) if data else {}
    oom = "OOMKilled" in res.stdout
    return (
        {"pod": pod, "namespace": namespace, "summary": summary, "oom_killed": oom, "describe": res.out[-8000:]},
        [res, json_res],
    )


# --------------------------------------------------------------------------
# Write-level tools
# --------------------------------------------------------------------------
@tool(
    name="restart_pod",
    description="Delete a pod so its controller recreates it. Reversible restart for a crashed or stuck pod.",
    category="kubernetes",
    level=Level.SAFE_WRITE,
    parameters={
        "type": "object",
        "properties": {
            "pod": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
        },
        "required": ["pod"],
    },
    timeout=120,
    risk_note="The pod is deleted; its Deployment/StatefulSet recreates it within seconds.",
)
def restart_pod(pod: str, namespace: str = DEFAULT_NAMESPACE) -> Tuple[Dict[str, Any], List[Any]]:
    data, get_res = _kubectl_json(["get", "pod", "-n", namespace, pod])
    if data is None:
        raise ToolError("pod %s/%s not found" % (namespace, pod))
    owners = data.get("metadata", {}).get("ownerReferences") or []
    if not owners:
        raise ToolError(
            "pod %s/%s has no controller; deleting it would not recreate it. Refusing." % (namespace, pod)
        )
    prefix = pod.rsplit("-", 1)[0]
    delete = _kubectl(["delete", "pod", "-n", namespace, pod, "--wait=false"], timeout=120)
    if not delete.ok:
        raise ToolError("delete failed: %s" % delete.stderr.strip()[:200])

    # VERIFY: poll until a replacement pod with the same controller prefix is
    # Ready, rather than reporting success the moment the delete call returns.
    deadline = time.time() + 120
    recreated: List[Dict[str, Any]] = []
    after_res = delete
    while time.time() < deadline:
        time.sleep(5)
        after, after_res = _kubectl_json(["get", "pods", "-n", namespace])
        recreated = [
            _summarise_pod(item)
            for item in (after or {}).get("items", [])
            if item["metadata"]["name"].startswith(prefix) and item["metadata"]["name"] != pod
        ]
        if recreated and any(item["healthy"] for item in recreated):
            break
    return (
        {
            "deleted_pod": pod,
            "namespace": namespace,
            "controller": owners[0].get("kind"),
            "replacement_pods": recreated,
            "verified_healthy": any(item["healthy"] for item in recreated),
        },
        [get_res, delete, after_res],
    )


@tool(
    name="scale_deployment",
    description="Change the replica count of a deployment. Requires approval.",
    category="kubernetes",
    level=Level.MEDIUM_RISK,
    parameters={
        "type": "object",
        "properties": {
            "deployment": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "replicas": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["deployment", "replicas"],
    },
    timeout=120,
    risk_note="Scaling to 0 takes the workload offline.",
)
def scale_deployment(deployment: str, replicas: int, namespace: str = DEFAULT_NAMESPACE) -> Tuple[Dict[str, Any], List[Any]]:
    before, before_res = _kubectl_json(["get", "deployment", "-n", namespace, deployment])
    if before is None:
        raise ToolError("deployment %s/%s not found" % (namespace, deployment))
    previous = before.get("spec", {}).get("replicas")
    res = _kubectl(["scale", "deployment", "-n", namespace, deployment, "--replicas", str(replicas)], timeout=120)
    if not res.ok:
        raise ToolError("scale failed: %s" % res.stderr.strip()[:200])
    after, after_res = _kubectl_json(["get", "deployment", "-n", namespace, deployment])
    return (
        {
            "deployment": deployment,
            "namespace": namespace,
            "replicas_before": previous,
            "replicas_after": (after or {}).get("spec", {}).get("replicas"),
            "ready": (after or {}).get("status", {}).get("readyReplicas", 0),
        },
        [before_res, res, after_res],
    )


@tool(
    name="set_deployment_resources",
    description="Change the memory limit of a container in a deployment. Used by the memory-pressure repair plan.",
    category="kubernetes",
    level=Level.MEDIUM_RISK,
    parameters={
        "type": "object",
        "properties": {
            "deployment": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253, "default": DEFAULT_NAMESPACE},
            "container": {"type": "string", "pattern": K8S_NAME, "maxLength": 253},
            "memory_limit": {"type": "string", "pattern": r"[0-9]{1,5}(Mi|Gi)", "maxLength": 10},
            "memory_request": {"type": "string", "pattern": r"[0-9]{1,5}(Mi|Gi)", "maxLength": 10},
        },
        "required": ["deployment", "container", "memory_limit"],
    },
    timeout=180,
    risk_note="The deployment restarts its pods to apply the new limits.",
)
def set_deployment_resources(deployment: str, container: str, memory_limit: str, namespace: str = DEFAULT_NAMESPACE, memory_request: Optional[str] = None) -> Tuple[Dict[str, Any], List[Any]]:
    resources: Dict[str, Any] = {"limits": {"memory": memory_limit}}
    if memory_request:
        resources["requests"] = {"memory": memory_request}
    patch = {"spec": {"template": {"spec": {"containers": [{"name": container, "resources": resources}]}}}}
    res = _kubectl(["patch", "deployment", "-n", namespace, deployment, "--type", "strategic", "-p", json.dumps(patch)], timeout=120)
    if not res.ok:
        raise ToolError("patch failed: %s" % res.stderr.strip()[:300])
    rollout = _kubectl(["rollout", "status", "deployment", "-n", namespace, deployment, "--timeout=150s"], timeout=180)
    after, after_res = _kubectl_json(["get", "deployment", "-n", namespace, deployment])
    return (
        {
            "deployment": deployment,
            "container": container,
            "memory_limit": memory_limit,
            "memory_request": memory_request,
            "rollout_complete": rollout.ok,
            "ready_replicas": (after or {}).get("status", {}).get("readyReplicas", 0),
        },
        [res, rollout, after_res],
    )


@tool(
    name="delete_kubernetes_namespace",
    description="Delete an entire namespace and everything in it. Highest risk operation available.",
    category="kubernetes",
    level=Level.HIGH_RISK,
    parameters={
        "type": "object",
        "properties": {"namespace": {"type": "string", "pattern": K8S_NAME, "maxLength": 253}},
        "required": ["namespace"],
    },
    timeout=300,
    risk_note="Every workload, secret and persistent volume claim in the namespace is destroyed. There is no undo.",
    autonomous=False,
)
def delete_kubernetes_namespace(namespace: str) -> Tuple[Dict[str, Any], List[Any]]:
    try:
        from agent.permissions import policy_document

        protected = policy_document().get("protected_namespaces") or []
    except Exception:  # noqa: BLE001
        protected = ["kube-system", "kube-public", "kube-node-lease", "default", "awx"]
    if namespace in protected:
        raise ToolError(
            "namespace %r is listed in policies/permissions.yaml -> protected_namespaces and can never be deleted by the agent" % namespace
        )
    before, before_res = _kubectl_json(["get", "all", "-n", namespace])
    res = _kubectl(["delete", "namespace", namespace, "--wait=false"], timeout=300)
    if not res.ok:
        raise ToolError("delete failed: %s" % res.stderr.strip()[:300])
    return (
        {"namespace": namespace, "deletion_started": True, "objects_before": len((before or {}).get("items", []))},
        [before_res, res],
    )
