#!/usr/bin/env python3
"""Smart AWX + AI Agent - single installer.

    sudo python3 install.py

Pipeline:

    DETECT -> COMPATIBILITY -> DEPENDENCIES -> K3S -> AWX OPERATOR -> AWX
           -> AI RUNTIME -> AI MODEL -> AI AGENT -> AI UI -> AWX INTEGRATION
           -> HEALTH CHECK -> FINAL REPORT

Every phase installs, verifies and health-checks.  A phase that fails collects
diagnostics, is diagnosed, recovered and retried at most three times.  Completed
phases are recorded in /var/lib/smart-awx-ai/state.json, so re-running the
installer resumes instead of reinstalling.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from installer import ai as ai_phase  # noqa: E402
from installer import awx as awx_phase  # noqa: E402
from installer import common  # noqa: E402
from installer import compatibility, dependencies, hardware, health, recovery  # noqa: E402
from installer import kubernetes as k8s_phase  # noqa: E402
from installer.common import (  # noqa: E402
    C,
    CompatibilityError,
    InstallError,
    Log,
    PHASES,
    REPO_ROOT,
    State,
    ensure_dirs,
    load_yaml,
    primary_ip,
    require_root,
    retry,
)

CONFIG_PATH = os.path.join(REPO_ROOT, "config", "compatibility.yaml")
DEFAULT_PROXY_NODEPORT = 30080
DEFAULT_AWX_NODEPORT = 30081


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install K3s + AWX + a local AI agent on this machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  sudo python3 install.py                        full install or resume
  sudo python3 install.py --dry-run              show what would run, change nothing
  sudo python3 install.py --status               health report for what is installed
  sudo python3 install.py --repair               re-run only the unhealthy phases
  sudo python3 install.py --keep-ufw             never touch the firewall
  sudo python3 install.py --operator-version 2.19.1
  sudo python3 install.py --phase ai_agent       re-run a single phase
""",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan without changing anything")
    parser.add_argument("--status", action="store_true", help="print the health report and exit")
    parser.add_argument("--repair", action="store_true", help="re-run the phases whose components are unhealthy")
    parser.add_argument("--keep-ufw", action="store_true", help="do not add firewall rules for the NodePorts")
    parser.add_argument("--operator-version", default=None, help="AWX Operator version (default from config/compatibility.yaml)")
    parser.add_argument("--model", default=None, help="force a specific Ollama model instead of the profile default")
    parser.add_argument("--profile", default=None, choices=["LOW", "TEST", "NORMAL", "HIGH"], help="override the detected hardware profile")
    parser.add_argument("--awx-nodeport", type=int, default=DEFAULT_AWX_NODEPORT, help="NodePort for AWX itself (default %d)" % DEFAULT_AWX_NODEPORT)
    parser.add_argument("--proxy-nodeport", type=int, default=DEFAULT_PROXY_NODEPORT, help="NodePort users open: AWX + AI Agent (default %d)" % DEFAULT_PROXY_NODEPORT)
    parser.add_argument("--phase", default=None, choices=[name for name, _ in PHASES], help="run one phase only")
    parser.add_argument("--skip-model", action="store_true", help="install the AI runtime but do not download a model")
    parser.add_argument("--force", action="store_true", help="continue even if a hard compatibility requirement fails")
    parser.add_argument("--reset-state", action="store_true", help="forget recorded phase state and start from phase 1")
    parser.add_argument("--retries", type=int, default=3, help="attempts per phase before giving up (default 3)")
    parser.add_argument("--verbose", "-v", action="store_true", help="print every command that runs")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Phase implementations
# --------------------------------------------------------------------------
class Installer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config: Dict[str, Any] = load_yaml(CONFIG_PATH)
        self.state = State.load()
        self.hardware: Optional[hardware.Hardware] = None

    # -- helpers -------------------------------------------------------
    @property
    def profile(self) -> str:
        return self.state.facts.get("profile") or (self.hardware.profile if self.hardware else "TEST")

    def run_phase(self, name: str, description: str, action: Callable[[], Dict[str, Any]], force: bool = False) -> None:
        if self.state.is_complete(name) and not force and not self.args.repair:
            Log.ok("phase %s is already complete - skipping (use --phase %s to force)" % (name, name))
            return
        Log.banner("PHASE: %s - %s" % (name.upper(), description))
        self.state.mark(name, "running")
        try:
            facts = retry(
                lambda attempt: action(),
                attempts=max(1, self.args.retries),
                on_failure=recovery.handle_failure(name),
                label="phase %s" % name,
            )
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            self.state.mark(name, "failed", str(exc)[:400])
            raise
        for key, value in (facts or {}).items():
            self.state.facts[key] = value
        self.state.mark(name, "complete")
        self.state.save()

    # -- phases --------------------------------------------------------
    def phase_preflight(self) -> Dict[str, Any]:
        detected = hardware.detect(self.config)
        if self.args.profile:
            Log.warn("overriding the detected profile %s with %s" % (detected.profile, self.args.profile))
            detected.profile = self.args.profile
        self.hardware = detected
        print(hardware.report(detected))
        print()
        checks = compatibility.evaluate(detected, self.config, force=self.args.force)
        compatibility.enforce(checks)
        sizing = (self.config.get("sizing", {}).get(detected.profile) or {})
        Log.info(
            "Profile %s selected: AWX web limit %s, AI model candidates %s"
            % (detected.profile, (sizing.get("awx") or {}).get("web_mem_limit"), ", ".join((sizing.get("models") or [])[:2]))
        )
        return {
            "profile": detected.profile,
            "hardware": detected.to_dict(),
            "host_ip": primary_ip(),
            "compatibility": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        }

    def phase_dependencies(self) -> Dict[str, Any]:
        return {"dependencies": dependencies.run_phase(self.config)}

    def phase_k3s(self) -> Dict[str, Any]:
        facts = k8s_phase.install_k3s(self.config, self.profile)
        if common.DRY_RUN:
            return facts
        checks = k8s_phase.verify()
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise InstallError("K3s installed but these checks failed: %s" % ", ".join(failed))
        facts["k3s_checks"] = checks
        return facts

    def phase_awx_operator(self) -> Dict[str, Any]:
        version = self.args.operator_version or self.config.get("versions", {}).get("awx_operator", "2.19.1")
        return awx_phase.install_operator(str(version))

    def phase_awx(self) -> Dict[str, Any]:
        facts = awx_phase.install_awx(self.profile, self.config, self.args.awx_nodeport, self.args.proxy_nodeport)
        url = "http://127.0.0.1:%d" % self.args.awx_nodeport
        facts["awx_url"] = url
        if not common.DRY_RUN:
            from installer.common import wait_for

            if not wait_for(lambda: awx_phase.api_reachable(url), timeout=600, interval=15, label="AWX API"):
                raise InstallError("AWX pods are ready but the API on %s never answered /api/v2/ping/" % url)
        password = awx_phase.admin_password()
        if not password and not common.DRY_RUN:
            raise InstallError("could not read the AWX admin password secret")
        Log.ok("AWX API answered on %s and the admin credentials were retrieved" % url)
        return facts

    def phase_ai_runtime(self) -> Dict[str, Any]:
        facts = ai_phase.install_runtime()
        if self.args.skip_model:
            Log.warn("--skip-model: no model will be downloaded; the agent will run in tool-only mode")
            return facts
        facts.update(ai_phase.select_model(self.profile, self.config, self.hardware or hardware.detect(self.config), self.args.model))
        return facts

    def phase_ai_agent(self) -> Dict[str, Any]:
        awx_url = self.state.facts.get("awx_url") or "http://127.0.0.1:%d" % self.args.awx_nodeport
        password = awx_phase.admin_password()
        model_info = {
            "ai_model": self.state.facts.get("ai_model", ""),
            "ai_model_fallbacks": self.state.facts.get("ai_model_fallbacks", []),
            "num_ctx": self.state.facts.get("num_ctx", 4096),
            "keep_alive": self.state.facts.get("keep_alive", "5m"),
        }
        facts = ai_phase.deploy_backend(self.profile, self.config, model_info, awx_url, password)
        ai_phase.apply_secret(awx_url, password)
        return facts

    def phase_ai_ui(self) -> Dict[str, Any]:
        host_ip = self.state.facts.get("host_ip") or primary_ip()
        facts = ai_phase.deploy_proxy(self.args.proxy_nodeport, host_ip)
        facts["firewall"] = ai_phase.configure_firewall(
            [self.args.proxy_nodeport, self.args.awx_nodeport], self.args.keep_ufw
        )
        return facts

    def phase_health(self) -> Dict[str, Any]:
        status = health.print_report(self.state, self.hardware)
        return {"final_status": status}

    # -- orchestration -------------------------------------------------
    def phase_map(self) -> Dict[str, Callable[[], Dict[str, Any]]]:
        return {
            "preflight": self.phase_preflight,
            "dependencies": self.phase_dependencies,
            "k3s": self.phase_k3s,
            "awx_operator": self.phase_awx_operator,
            "awx": self.phase_awx,
            "ai_runtime": self.phase_ai_runtime,
            "ai_agent": self.phase_ai_agent,
            "ai_ui": self.phase_ai_ui,
            "health": self.phase_health,
        }

    def install(self) -> int:
        mapping = self.phase_map()
        only = self.args.phase
        if only:
            Log.warn("running a single phase: %s" % only)
            if only != "preflight":
                self.hardware = hardware.detect(self.config)
                if self.args.profile:
                    self.hardware.profile = self.args.profile
            self.run_phase(only, dict(PHASES)[only], mapping[only], force=True)
            return 0

        for name, description in PHASES:
            self.run_phase(name, description, mapping[name])
        return 0

    # Detection and compatibility are deterministic: retrying them cannot help.

    def repair(self) -> int:
        """Re-run only the phases whose components are currently unhealthy."""
        Log.banner("REPAIR - checking what is actually broken")
        self.hardware = hardware.detect(self.config)
        rows = health.collect(self.state, self.hardware)
        broken_groups = {row.group for row in rows if row.status in (health.DEGRADED, health.MISSING)}
        print(health.render(rows, self.state))

        group_to_phases = {
            "KUBERNETES": ["k3s"],
            "AWX": ["awx_operator", "awx"],
            "AI": ["ai_runtime", "ai_agent", "ai_ui"],
            "INTEGRATION": ["ai_agent", "ai_ui"],
            "SYSTEM": [],
        }
        targets: List[str] = []
        for group in broken_groups:
            for phase in group_to_phases.get(group, []):
                if phase not in targets:
                    targets.append(phase)
        targets.sort(key=lambda name: [p for p, _ in PHASES].index(name))

        if not targets:
            Log.ok("nothing to repair - every component is healthy")
            return 0

        Log.info("repairing phases: %s" % ", ".join(targets))
        mapping = self.phase_map()
        for name in targets:
            self.state.mark(name, "pending")
            self.run_phase(name, dict(PHASES)[name], mapping[name], force=True)
        self.phase_health()
        return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    common.DRY_RUN = args.dry_run
    Log.verbose = args.verbose

    if args.dry_run:
        print(C.p(C.YELLOW, "DRY RUN: detection and read-only probes run; nothing is installed or changed.\n"))
    else:
        try:
            require_root()
        except InstallError as exc:
            print(C.p(C.RED, str(exc)), file=sys.stderr)
            return 2

    if args.dry_run:
        # A dry run creates nothing: only attach the file log if the directory
        # already exists from a previous real run.
        if os.path.isdir(common.LOG_DIR):
            Log.open()
    else:
        ensure_dirs()
        Log.open()

    try:
        installer = Installer(args)
    except OSError as exc:
        print(C.p(C.RED, "could not read %s: %s" % (CONFIG_PATH, exc)), file=sys.stderr)
        return 2

    if args.reset_state:
        installer.state = State(path=installer.state.path)
        for name, _ in PHASES:
            installer.state.phases[name] = common.PhaseState()
        installer.state.save()
        Log.warn("phase state reset")

    if args.status:
        installer.hardware = hardware.detect(installer.config)
        status = health.print_report(installer.state, installer.hardware)
        return 0 if status == health.HEALTHY else 1

    Log.banner("SMART AWX + AI AGENT INSTALLER")
    Log.info("Log file: %s" % common.INSTALL_LOG)
    Log.info("State   : %s" % installer.state.path)
    completed = [name for name, _ in PHASES if installer.state.is_complete(name)]
    if completed:
        Log.info("Already complete: %s" % ", ".join(completed))

    try:
        if args.repair:
            return installer.repair()
        return installer.install()
    except CompatibilityError as exc:
        Log.error(str(exc))
        return 3
    except InstallError as exc:
        Log.error(str(exc))
        Log.info("Diagnostics and logs: %s" % common.LOG_DIR)
        Log.info("Fix the cause and re-run `sudo python3 install.py` - completed phases are not redone.")
        return 1
    except KeyboardInterrupt:
        Log.warn("interrupted - progress up to the last completed phase is saved")
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, with a real traceback
        Log.error("unexpected error: %s" % exc)
        Log.debug(traceback.format_exc())
        with open(os.path.join(common.LOG_DIR, "install.log"), "a", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
