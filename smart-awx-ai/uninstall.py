#!/usr/bin/env python3
"""Remove the Smart AWX + AI Agent stack.

    sudo python3 uninstall.py                 # agent + proxy + AWX, keep K3s and Ollama
    sudo python3 uninstall.py --all           # everything, including K3s and the models
    sudo python3 uninstall.py --dry-run       # show what would be removed

Removal is staged from the top down so nothing is orphaned, and each stage is
confirmed unless --yes is given.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from installer import common  # noqa: E402
from installer.common import (  # noqa: E402
    AGENT_SERVICE,
    AWX_NAMESPACE,
    C,
    CONFIG_DIR,
    INSTALL_ROOT,
    LOG_DIR,
    STATE_DIR,
    Log,
    require_root,
    run,
    which,
)
from installer.kubernetes import kubectl  # noqa: E402


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes or common.DRY_RUN:
        return True
    try:
        answer = input("%s [y/N] " % question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def remove_agent_service() -> None:
    Log.banner("Removing the AI Agent backend")
    run(["systemctl", "disable", "--now", AGENT_SERVICE], timeout=120)
    unit = "/etc/systemd/system/%s.service" % AGENT_SERVICE
    if os.path.exists(unit) and not common.DRY_RUN:
        os.remove(unit)
    run(["systemctl", "daemon-reload"], timeout=60)
    Log.ok("agent service removed")


def remove_cluster_objects(remove_awx: bool) -> None:
    Log.banner("Removing Kubernetes objects")
    if not (which("kubectl") or which("k3s")):
        Log.warn("kubectl is not available; skipping cluster cleanup")
        return
    for args in (
        ["delete", "deployment", "awx-ai-proxy", "-n", AWX_NAMESPACE, "--ignore-not-found"],
        ["delete", "service", "awx-ai-proxy", "-n", AWX_NAMESPACE, "--ignore-not-found"],
        ["delete", "configmap", "awx-ai-proxy-config", "-n", AWX_NAMESPACE, "--ignore-not-found"],
        ["delete", "secret", "smart-awx-ai-credentials", "-n", AWX_NAMESPACE, "--ignore-not-found"],
    ):
        kubectl(args, timeout=180)
    Log.ok("AI proxy objects removed")

    if remove_awx:
        Log.step("deleting the AWX custom resource and its namespace (this destroys AWX data)")
        kubectl(["delete", "awx", "awx", "-n", AWX_NAMESPACE, "--ignore-not-found"], timeout=300)
        kubectl(["delete", "namespace", AWX_NAMESPACE, "--ignore-not-found"], timeout=600)
        Log.ok("AWX removed")


def remove_k3s() -> None:
    Log.banner("Removing K3s")
    script = "/usr/local/bin/k3s-uninstall.sh"
    if os.path.exists(script):
        res = run([script], timeout=900)
        if res.ok:
            Log.ok("k3s uninstalled")
            return
        Log.warn("the k3s uninstall script failed:\n%s" % res.tail(15))
    else:
        Log.warn("%s not found; k3s may not have been installed by this installer" % script)


def remove_models(remove_runtime: bool) -> None:
    Log.banner("Removing AI models")
    if not which("ollama"):
        Log.warn("ollama is not installed")
        return
    listing = run(["ollama", "list"], mutating=False, timeout=60)
    for line in listing.out.splitlines()[1:]:
        name = line.split()[0] if line.split() else ""
        if name:
            run(["ollama", "rm", name], timeout=300)
    Log.ok("models removed")
    if remove_runtime:
        run(["systemctl", "disable", "--now", "ollama"], timeout=120)
        for path in ("/usr/local/bin/ollama", "/usr/bin/ollama", "/etc/systemd/system/ollama.service"):
            if os.path.exists(path) and not common.DRY_RUN:
                try:
                    os.remove(path)
                except OSError as exc:
                    Log.warn("could not remove %s: %s" % (path, exc))
        run(["systemctl", "daemon-reload"], timeout=60)
        Log.ok("ollama runtime removed (the model cache in /usr/share/ollama is left in place)")


def remove_files(keep_logs: bool) -> None:
    Log.banner("Removing installed files")
    targets: List[str] = [INSTALL_ROOT, CONFIG_DIR, STATE_DIR]
    if not keep_logs:
        targets.append(LOG_DIR)
    for path in targets:
        if not os.path.exists(path):
            continue
        if common.DRY_RUN:
            Log.info("dry run: would remove %s" % path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        Log.step("removed %s" % path)
    Log.ok("files removed%s" % (" (logs kept in %s)" % LOG_DIR if keep_logs else ""))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="uninstall.py", description="Remove the Smart AWX + AI Agent stack.")
    parser.add_argument("--all", action="store_true", help="also remove AWX, K3s, the models and the Ollama runtime")
    parser.add_argument("--keep-awx", action="store_true", help="leave AWX and its namespace in place")
    parser.add_argument("--keep-k3s", action="store_true", help="leave the K3s cluster in place")
    parser.add_argument("--keep-models", action="store_true", help="leave the downloaded AI models in place")
    parser.add_argument("--keep-logs", action="store_true", help="leave %s in place" % LOG_DIR)
    parser.add_argument("--dry-run", action="store_true", help="show what would be removed")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    common.DRY_RUN = args.dry_run
    if not args.dry_run:
        try:
            require_root()
        except Exception as exc:  # noqa: BLE001
            print(C.p(C.RED, str(exc)), file=sys.stderr)
            return 2
    Log.open(os.path.join(LOG_DIR, "uninstall.log") if os.path.isdir(LOG_DIR) else "/tmp/smart-awx-ai-uninstall.log")

    remove_awx = args.all and not args.keep_awx
    remove_k3s_flag = args.all and not args.keep_k3s
    remove_model_flag = args.all and not args.keep_models

    print(C.p(C.BOLD, "This will remove:"))
    print("  - the AI Agent backend service and its files")
    print("  - the AI proxy deployment, service, configmap and secret")
    print("  - AWX and its namespace" if remove_awx else "  - (AWX will be kept)")
    print("  - K3s and every workload on it" if remove_k3s_flag else "  - (K3s will be kept)")
    print("  - the downloaded AI models" if remove_model_flag else "  - (AI models will be kept)")
    print()
    if not confirm("Continue?", args.yes):
        print("Cancelled.")
        return 0

    remove_agent_service()
    remove_cluster_objects(remove_awx)
    if remove_model_flag:
        remove_models(remove_runtime=True)
    if remove_k3s_flag:
        remove_k3s()
    remove_files(args.keep_logs)

    Log.ok("uninstall complete")
    if not args.all:
        print("\nK3s, AWX and the AI models were kept. Re-run with --all to remove them too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
