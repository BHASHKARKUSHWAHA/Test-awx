"""Phases 3 and 4 - AWX Operator and the AWX instance (spec sections 7 and 8).

The operator is installed with ``kubectl apply -k`` against the upstream
``ansible/awx-operator`` kustomize base at a pinned tag, so there is no vendored
copy to drift.  The AWX CR itself is rendered from ``manifests/awx.yaml`` using
the sizing block for the detected hardware profile.
"""

from __future__ import annotations

import base64
import os
import string
from typing import Any, Dict, List, Optional

from . import common
from .common import (
    AWX_NAMESPACE,
    INSTALL_ROOT,
    REPO_ROOT,
    InstallError,
    Log,
    run,
    wait_for,
)
from .kubernetes import ensure_namespace, kubectl, kubectl_json

OPERATOR_REPO = "github.com/ansible/awx-operator"
OPERATOR_DIR = os.path.join(INSTALL_ROOT, "awx-operator")
AWX_NAME = "awx"


# --------------------------------------------------------------------------
# Operator
# --------------------------------------------------------------------------
def _operator_kustomization(version: str) -> str:
    return (
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - %s/config/default?ref=%s\n"
        "images:\n"
        "  - name: quay.io/ansible/awx-operator\n"
        "    newTag: %s\n"
        "namespace: %s\n" % (OPERATOR_REPO, version, version, AWX_NAMESPACE)
    )


def operator_deployment_ready() -> bool:
    data = kubectl_json(["get", "deployment", "awx-operator-controller-manager", "-n", AWX_NAMESPACE], timeout=60)
    if not data:
        return False
    status = data.get("status", {})
    return status.get("readyReplicas", 0) >= 1 and status.get("availableReplicas", 0) >= 1


def crd_present() -> bool:
    res = kubectl(["get", "crd", "awxs.awx.ansible.com"], mutating=False, timeout=60)
    return res.ok


def install_operator(version: str) -> Dict[str, Any]:
    if common.DRY_RUN:
        Log.info("dry run: would apply %s/config/default?ref=%s into namespace %s" % (OPERATOR_REPO, version, AWX_NAMESPACE))
        return {"operator_version": version, "operator_image": "(dry-run)"}
    ensure_namespace(AWX_NAMESPACE)
    os.makedirs(OPERATOR_DIR, exist_ok=True)
    path = os.path.join(OPERATOR_DIR, "kustomization.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_operator_kustomization(version))

    Log.step("applying awx-operator %s via kustomize" % version)
    res = kubectl(["apply", "-k", OPERATOR_DIR], timeout=1200)
    if not res.ok:
        raise InstallError("kubectl apply -k failed for awx-operator %s:\n%s" % (version, res.tail(30)))

    if not wait_for(crd_present, timeout=300, interval=5, label="AWX CRD"):
        raise InstallError("the awxs.awx.ansible.com CRD never appeared after applying the operator")

    if not wait_for(
        operator_deployment_ready,
        timeout=900,
        interval=10,
        label="operator deployment",
        progress=lambda left: "waiting for awx-operator-controller-manager (%ds left)" % left,
    ):
        raise InstallError(
            "the awx-operator deployment never became ready:\n%s"
            % kubectl(["describe", "deployment", "awx-operator-controller-manager", "-n", AWX_NAMESPACE], mutating=False).tail(30)
        )

    image = "unknown"
    data = kubectl_json(["get", "deployment", "awx-operator-controller-manager", "-n", AWX_NAMESPACE], timeout=60)
    if data:
        for container in data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            if "awx-operator" in container.get("image", ""):
                image = container["image"]
    Log.ok("AWX Operator ready (%s)" % image)
    return {"operator_version": version, "operator_image": image}


# --------------------------------------------------------------------------
# AWX instance
# --------------------------------------------------------------------------
def render_awx_manifest(profile: str, config: Dict[str, Any], awx_nodeport: int, proxy_nodeport: int, storage_class: str) -> str:
    sizing = (config.get("sizing", {}).get(profile) or {}).get("awx", {})
    template = string.Template(open(os.path.join(REPO_ROOT, "manifests", "awx.yaml"), encoding="utf-8").read())
    return template.safe_substitute(
        AWX_NAME=AWX_NAME,
        AWX_NAMESPACE=AWX_NAMESPACE,
        PROFILE=profile,
        AWX_NODEPORT=awx_nodeport,
        PROXY_NODEPORT=proxy_nodeport,
        REPLICAS=sizing.get("replicas", 1),
        STORAGE_CLASS=storage_class,
        POSTGRES_STORAGE=sizing.get("postgres_storage", "8Gi"),
        WEB_MEM_REQUEST=sizing.get("web_mem_request", "768Mi"),
        WEB_MEM_LIMIT=sizing.get("web_mem_limit", "1536Mi"),
        TASK_MEM_REQUEST=sizing.get("task_mem_request", "768Mi"),
        TASK_MEM_LIMIT=sizing.get("task_mem_limit", "1536Mi"),
        EE_MEM_LIMIT=sizing.get("ee_mem_limit", "768Mi"),
        POSTGRES_MEM_LIMIT=sizing.get("postgres_mem_limit", "768Mi"),
    )


def default_storage_class() -> str:
    data = kubectl_json(["get", "storageclass"], timeout=60) or {}
    items = data.get("items") or []
    for item in items:
        annotations = item.get("metadata", {}).get("annotations", {}) or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return item["metadata"]["name"]
    if items:
        return items[0]["metadata"]["name"]
    return "local-path"


def awx_pods() -> List[Dict[str, Any]]:
    data = kubectl_json(["get", "pods", "-n", AWX_NAMESPACE, "-l", "app.kubernetes.io/managed-by=awx-operator"], timeout=60)
    if not data:
        data = kubectl_json(["get", "pods", "-n", AWX_NAMESPACE], timeout=60) or {}
    return data.get("items") or []


def _pod_ready(pod: Dict[str, Any]) -> bool:
    conditions = {c["type"]: c["status"] for c in pod.get("status", {}).get("conditions", [])}
    return conditions.get("Ready") == "True"


def awx_ready() -> bool:
    """AWX is ready when the web and task deployments both report ready pods."""
    for suffix in ("web", "task"):
        data = kubectl_json(["get", "deployment", "%s-%s" % (AWX_NAME, suffix), "-n", AWX_NAMESPACE], timeout=60)
        if not data:
            return False
        if (data.get("status", {}).get("readyReplicas") or 0) < 1:
            return False
    return True


def _progress_note(left: int) -> str:
    pods = awx_pods()
    if not pods:
        return "waiting for the operator to create AWX pods (%ds left)" % left
    summary = ", ".join(
        "%s=%s" % (p["metadata"]["name"], p.get("status", {}).get("phase", "?")) for p in pods
    )
    return "AWX pods: %s (%ds left)" % (summary, left)


def install_awx(
    profile: str,
    config: Dict[str, Any],
    awx_nodeport: int,
    proxy_nodeport: int,
    timeout: int = 2400,
) -> Dict[str, Any]:
    storage_class = default_storage_class()
    manifest = render_awx_manifest(profile, config, awx_nodeport, proxy_nodeport, storage_class)
    if common.DRY_RUN:
        Log.info("dry run: would apply this AWX custom resource (profile %s):" % profile)
        print("\n".join("    " + line for line in manifest.splitlines() if line.strip() and not line.strip().startswith("#")))
        return {"awx_name": AWX_NAME, "awx_namespace": AWX_NAMESPACE, "awx_nodeport": awx_nodeport, "storage_class": storage_class}
    path = os.path.join(OPERATOR_DIR, "awx-instance.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(manifest)

    Log.step("applying the AWX custom resource (profile=%s, storageClass=%s, nodePort=%d)" % (profile, storage_class, awx_nodeport))
    res = kubectl(["apply", "-f", path], timeout=300)
    if not res.ok:
        raise InstallError("could not apply the AWX custom resource:\n%s" % res.tail(30))

    Log.info("AWX first boot pulls several container images and runs database migrations - this takes 10-25 minutes on an 8 GB machine.")
    if not wait_for(awx_ready, timeout=timeout, interval=15, label="AWX deployments", progress=_progress_note):
        raise InstallError("AWX did not become ready within %d seconds:\n%s" % (timeout, diagnostics()))

    info = {"awx_name": AWX_NAME, "awx_namespace": AWX_NAMESPACE, "awx_nodeport": awx_nodeport, "storage_class": storage_class}
    info.update(service_info())
    Log.ok("AWX is running (service %s)" % info.get("service_name"))
    return info


def service_info() -> Dict[str, Any]:
    data = kubectl_json(["get", "svc", "-n", AWX_NAMESPACE], timeout=60) or {}
    for item in data.get("items") or []:
        name = item["metadata"]["name"]
        if name in ("%s-service" % AWX_NAME, AWX_NAME):
            ports = item.get("spec", {}).get("ports") or [{}]
            return {
                "service_name": name,
                "service_type": item.get("spec", {}).get("type"),
                "cluster_ip": item.get("spec", {}).get("clusterIP"),
                "port": ports[0].get("port"),
                "node_port": ports[0].get("nodePort"),
            }
    return {}


def admin_password() -> Optional[str]:
    """Read the admin password the operator generated (never logged in clear)."""
    data = kubectl_json(["get", "secret", "%s-admin-password" % AWX_NAME, "-n", AWX_NAMESPACE], timeout=60)
    if not data:
        return None
    encoded = (data.get("data") or {}).get("password")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def api_reachable(base_url: str) -> bool:
    res = run(
        ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", base_url.rstrip("/") + "/api/v2/ping/"],
        mutating=False,
        timeout=30,
    )
    return res.ok and res.out.startswith("2")


def verify(base_url: str) -> Dict[str, Any]:
    checks = {
        "operator_ready": operator_deployment_ready(),
        "awx_deployments_ready": awx_ready(),
        "admin_secret_present": admin_password() is not None,
        "api_ping": api_reachable(base_url),
    }
    return checks


def diagnostics() -> str:
    parts = []
    for label, args in (
        ("kubectl get pods -n awx", ["get", "pods", "-n", AWX_NAMESPACE, "-o", "wide"]),
        ("kubectl get events -n awx", ["get", "events", "-n", AWX_NAMESPACE, "--sort-by=.lastTimestamp"]),
        ("kubectl get awx -n awx", ["get", "awx", "-n", AWX_NAMESPACE, "-o", "yaml"]),
        ("kubectl get pvc -n awx", ["get", "pvc", "-n", AWX_NAMESPACE]),
    ):
        res = kubectl(args, mutating=False, timeout=90)
        parts.append("### %s\n%s" % (label, res.tail(40)))
    for pod in awx_pods():
        name = pod["metadata"]["name"]
        if _pod_ready(pod):
            continue
        logs = kubectl(["logs", "-n", AWX_NAMESPACE, name, "--all-containers", "--tail", "40"], mutating=False, timeout=90)
        parts.append("### logs %s\n%s" % (name, logs.tail(40)))
    return "\n\n".join(parts)


def recover(attempt: int) -> None:
    """Delete only pods that are actually broken; never delete the AWX CR."""
    Log.step("recovery attempt %d: inspecting AWX pods" % attempt)
    for pod in awx_pods():
        name = pod["metadata"]["name"]
        statuses = pod.get("status", {}).get("containerStatuses") or []
        broken = any(
            (cs.get("state", {}).get("waiting") or {}).get("reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError")
            for cs in statuses
        )
        if broken:
            Log.step("deleting unhealthy pod %s so the controller recreates it" % name)
            kubectl(["delete", "pod", "-n", AWX_NAMESPACE, name], timeout=120)
    kubectl(["rollout", "restart", "deployment", "awx-operator-controller-manager", "-n", AWX_NAMESPACE], timeout=120)
