"""Phase 2 - K3s single node Kubernetes (spec section 6).

K3s is used instead of kubeadm because the reference machine is an 8 GB box:
one binary, one systemd unit, embedded containerd and SQLite instead of etcd.
Traefik and servicelb are disabled because the AI proxy owns the NodePort entry
point; the local-path provisioner stays enabled because AWX needs a PVC for
PostgreSQL.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from . import common
from .common import (
    INSTALL_ROOT,
    KUBECONFIG,
    InstallError,
    Log,
    Result,
    run,
    wait_for,
    which,
)

K3S_INSTALL_URL = "https://get.k3s.io"
K3S_SCRIPT = os.path.join(INSTALL_ROOT, "cache", "k3s-install.sh")


# --------------------------------------------------------------------------
# kubectl plumbing
# --------------------------------------------------------------------------
def kube_env() -> Dict[str, str]:
    return {"KUBECONFIG": KUBECONFIG}


def kubectl(args: List[str], timeout: int = 120, mutating: bool = True, check: bool = False) -> Result:
    """Run kubectl via the k3s binary when a standalone kubectl is absent."""
    if which("kubectl"):
        cmd = ["kubectl"] + args
    elif which("k3s"):
        cmd = ["k3s", "kubectl"] + args
    else:
        return Result(127, "", "neither kubectl nor k3s is installed", ["kubectl"] + args)
    return run(cmd, timeout=timeout, env=kube_env(), mutating=mutating, check=check)


def kubectl_json(args: List[str], timeout: int = 120) -> Optional[Any]:
    res = kubectl(args + ["-o", "json"], timeout=timeout, mutating=False)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)
    except ValueError:
        return None


def node_ready() -> bool:
    data = kubectl_json(["get", "nodes"], timeout=60)
    if not data or not data.get("items"):
        return False
    for node in data["items"]:
        conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        if conditions.get("Ready") != "True":
            return False
    return True


def service_active(unit: str) -> bool:
    res = run(["systemctl", "is-active", unit], mutating=False, timeout=20)
    return res.out == "active"


def ensure_namespace(name: str) -> None:
    res = kubectl(["get", "namespace", name], mutating=False, timeout=60)
    if res.ok:
        return
    create = kubectl(["create", "namespace", name], timeout=60)
    if not create.ok and "AlreadyExists" not in create.stderr:
        raise InstallError("could not create namespace %s: %s" % (name, create.tail(10)))


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------
def _download_installer() -> None:
    os.makedirs(os.path.dirname(K3S_SCRIPT), exist_ok=True)
    Log.step("downloading the K3s installer")
    res = run(["curl", "-sfL", "--retry", "3", "--retry-delay", "5", "-o", K3S_SCRIPT, K3S_INSTALL_URL], timeout=300)
    if not res.ok or (os.path.exists(K3S_SCRIPT) and os.path.getsize(K3S_SCRIPT) < 1000):
        raise InstallError("could not download the K3s installer from %s: %s" % (K3S_INSTALL_URL, res.tail(10)))
    os.chmod(K3S_SCRIPT, 0o700)


def install_k3s(config: Dict[str, Any], profile: str) -> Dict[str, Any]:
    exec_flags = (config.get("versions", {}).get("k3s_exec") or {}).get(profile)
    if not exec_flags:
        exec_flags = "--disable traefik --disable servicelb --write-kubeconfig-mode 644"

    if common.DRY_RUN:
        Log.info("dry run: would install k3s (channel %s) with: server %s" % (config.get("versions", {}).get("k3s_channel", "stable"), exec_flags))
        Log.info("dry run: would then wait for the node to report Ready and CoreDNS to be available")
        return {"k3s_version": "(dry-run)", "exec_flags": exec_flags, "k3s_checks": {}}

    if which("k3s") and service_active("k3s"):
        Log.step("k3s is already installed and active - verifying instead of reinstalling")
    else:
        _download_installer()
        Log.step("installing k3s (%s)" % exec_flags)
        env = {
            "INSTALL_K3S_EXEC": "server " + exec_flags,
            "INSTALL_K3S_CHANNEL": str(config.get("versions", {}).get("k3s_channel", "stable")),
        }
        res = run(["sh", K3S_SCRIPT], env=env, timeout=1800)
        if not res.ok:
            raise InstallError("the K3s installer failed:\n%s" % res.tail(30))

    run(["systemctl", "enable", "--now", "k3s"], timeout=300)

    if not wait_for(lambda: service_active("k3s"), timeout=180, interval=5, label="k3s service"):
        raise InstallError("the k3s systemd unit did not become active: %s" % run(["systemctl", "status", "k3s", "--no-pager"], mutating=False).tail(20))

    if not wait_for(
        node_ready,
        timeout=420,
        interval=10,
        label="node Ready",
        progress=lambda left: "waiting for the node to report Ready (%ds left)" % left,
    ):
        raise InstallError("the Kubernetes node never reached Ready:\n%s" % kubectl(["get", "nodes", "-o", "wide"], mutating=False).tail(10))

    # Make kubectl usable for an interactive root shell too.
    if not which("kubectl") and which("k3s"):
        link = "/usr/local/bin/kubectl"
        if not os.path.exists(link):
            try:
                os.symlink("/usr/local/bin/k3s", link)
            except OSError as exc:
                Log.debug("could not symlink kubectl: %s" % exc)

    version = kubectl(["version", "-o", "json"], mutating=False, timeout=60)
    server_version = "unknown"
    try:
        server_version = json.loads(version.stdout).get("serverVersion", {}).get("gitVersion", "unknown")
    except ValueError:
        pass

    wait_for(core_dns_ready, timeout=300, interval=10, label="CoreDNS")
    Log.ok("K3s %s is running and the node is Ready" % server_version)
    return {"k3s_version": server_version, "exec_flags": exec_flags}


def core_dns_ready() -> bool:
    data = kubectl_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns"], timeout=60)
    if not data:
        return False
    items = data.get("items") or []
    return bool(items) and all(
        all(cs.get("ready") for cs in (pod.get("status", {}).get("containerStatuses") or []))
        for pod in items
    )


# --------------------------------------------------------------------------
# Verify / diagnose
# --------------------------------------------------------------------------
def verify() -> Dict[str, Any]:
    checks = {
        "k3s_service_active": service_active("k3s"),
        "node_ready": node_ready(),
        "api_reachable": kubectl(["get", "--raw", "/readyz"], mutating=False, timeout=30).ok,
        "coredns_ready": core_dns_ready(),
        "storage_class": False,
    }
    data = kubectl_json(["get", "storageclass"], timeout=60)
    if data:
        checks["storage_class"] = bool(data.get("items"))
    return checks


def diagnostics() -> str:
    parts = []
    for label, cmd in (
        ("systemctl status k3s", ["systemctl", "status", "k3s", "--no-pager", "-l"]),
        ("journalctl -u k3s (last 40)", ["journalctl", "-u", "k3s", "-n", "40", "--no-pager"]),
    ):
        res = run(cmd, mutating=False, timeout=60)
        parts.append("### %s\n%s" % (label, res.tail(40)))
    for label, args in (
        ("kubectl get nodes", ["get", "nodes", "-o", "wide"]),
        ("kubectl get pods -A", ["get", "pods", "-A"]),
        ("kubectl get events -A", ["get", "events", "-A", "--sort-by=.lastTimestamp"]),
    ):
        res = kubectl(args, mutating=False, timeout=60)
        parts.append("### %s\n%s" % (label, res.tail(30)))
    return "\n\n".join(parts)


def recover(attempt: int) -> None:
    """Safe recovery between retries: restart the unit, never wipe the cluster."""
    Log.step("recovery attempt %d: restarting k3s" % attempt)
    run(["systemctl", "restart", "k3s"], timeout=300)
    wait_for(node_ready, timeout=180, interval=10, label="node Ready after restart")
