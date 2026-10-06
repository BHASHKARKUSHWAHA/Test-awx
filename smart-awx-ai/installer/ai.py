"""Phases 4-7: AI runtime, local model, agent backend, UI and AWX integration.

Design decisions worth knowing before reading the code:

* **Ollama on the host, not in the cluster.** The model needs page cache and
  predictable memory on an 8 GB box; a pod competing with AWX for the same
  memory is how you get OOM kills.
* **The agent backend is a host systemd service.** Linux tools must see the
  real host - its PIDs, units, journal and memory - not a container's view.
* **The UI is exposed through an nginx proxy in the cluster** that also injects
  one script tag into AWX's HTML. The AWX image is never modified.
"""

from __future__ import annotations

import json
import os
import shutil
import secrets
import string as string_module
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import common
from .common import (
    AGENT_ENV_FILE,
    AGENT_PORT,
    AGENT_SERVICE,
    AWX_NAMESPACE,
    CONFIG_DIR,
    INSTALL_ROOT,
    KUBECONFIG,
    LOG_DIR,
    REPO_ROOT,
    STATE_DIR,
    InstallError,
    Log,
    primary_ip,
    run,
    wait_for,
    which,
    write_file,
)
from .kubernetes import kubectl, kubectl_json

OLLAMA_INSTALL_URL = "https://ollama.com/install.sh"
OLLAMA_SCRIPT = os.path.join(INSTALL_ROOT, "cache", "ollama-install.sh")
OLLAMA_URL = "http://127.0.0.1:11434"
VENV = os.path.join(INSTALL_ROOT, "venv")
PROXY_IMAGE = os.environ.get("AI_PROXY_IMAGE", "docker.io/library/nginx:1.27-alpine")


# --------------------------------------------------------------------------
# Phase 4a - AI runtime
# --------------------------------------------------------------------------
def ollama_reachable(timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def install_runtime() -> Dict[str, Any]:
    if common.DRY_RUN:
        Log.info("dry run: would install Ollama from %s and enable its systemd unit" % OLLAMA_INSTALL_URL)
        return {"ollama_version": "(dry-run)", "ollama_url": OLLAMA_URL}
    if which("ollama"):
        Log.step("ollama is already installed - verifying instead of reinstalling")
    else:
        Log.step("downloading the Ollama installer")
        os.makedirs(os.path.dirname(OLLAMA_SCRIPT), exist_ok=True)
        res = run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "5", "-o", OLLAMA_SCRIPT, OLLAMA_INSTALL_URL], timeout=300)
        if not res.ok:
            raise InstallError("could not download the Ollama installer: %s" % res.tail(10))
        os.chmod(OLLAMA_SCRIPT, 0o700)
        Log.step("installing Ollama (this pulls a ~1 GB runtime)")
        install = run(["sh", OLLAMA_SCRIPT], timeout=1800)
        if not install.ok:
            raise InstallError("the Ollama installer failed:\n%s" % install.tail(30))

    run(["systemctl", "enable", "--now", "ollama"], timeout=180)
    if not wait_for(ollama_reachable, timeout=180, interval=5, label="ollama API"):
        status = run(["systemctl", "status", "ollama", "--no-pager", "-l"], mutating=False, timeout=30)
        raise InstallError("the Ollama API never became reachable on %s:\n%s" % (OLLAMA_URL, status.tail(25)))

    version = run(["ollama", "--version"], mutating=False, timeout=30)
    Log.ok("Ollama is running (%s)" % (version.out or "version unknown"))
    return {"ollama_version": version.out, "ollama_url": OLLAMA_URL}


# --------------------------------------------------------------------------
# Phase 4b - local model (spec sections 13, 14)
# --------------------------------------------------------------------------
def installed_models() -> List[str]:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return [m.get("name", "") for m in payload.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []


def model_answers(model: str, timeout: int = 180) -> Tuple[bool, str]:
    """Prove the model actually runs here instead of trusting the pull."""
    body = json.dumps(
        {
            "model": model,
            "prompt": "Reply with the single word: ready",
            "stream": False,
            "options": {"num_predict": 8, "temperature": 0},
        }
    ).encode()
    request = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        text = (payload.get("response") or "").strip()
        return bool(text), text[:120]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return False, str(exc)


def select_model(profile: str, config: Dict[str, Any], hardware, requested: Optional[str] = None) -> Dict[str, Any]:
    """Pull the first model for this profile that downloads *and* answers."""
    sizing = (config.get("sizing", {}).get(profile) or {})
    candidates: List[str] = list(sizing.get("models") or [])
    if requested:
        candidates = [requested] + [m for m in candidates if m != requested]
    if not candidates:
        raise InstallError("no models are configured for profile %s in config/compatibility.yaml" % profile)

    if common.DRY_RUN:
        Log.info("dry run: would try these models in order and keep the first that answers: %s" % ", ".join(candidates))
        sizing_ollama = sizing.get("ollama") or {}
        return {
            "ai_model": candidates[0],
            "ai_model_fallbacks": candidates[1:],
            "attempts": [],
            "num_ctx": sizing_ollama.get("num_ctx", 4096),
            "keep_alive": sizing_ollama.get("keep_alive", "5m"),
        }

    guard_gb = float(config.get("model_disk_guard_gb", 12))
    free_gb = hardware.disk.get("free_gb") or 0
    if free_gb < guard_gb:
        raise InstallError(
            "only %.1f GB free; the model disk guard requires %.1f GB. Free space or lower model_disk_guard_gb." % (free_gb, guard_gb)
        )

    present = installed_models()
    attempts: List[Dict[str, Any]] = []

    for model in candidates:
        if model in present:
            Log.step("model %s is already present - verifying it answers" % model)
        else:
            Log.step("pulling model %s (profile %s)" % (model, profile))
            pull = run(["ollama", "pull", model], timeout=3600)
            if not pull.ok:
                reason = (pull.stderr or pull.stdout).strip()[-300:]
                Log.warn("could not pull %s: %s" % (model, reason))
                attempts.append({"model": model, "pulled": False, "reason": reason})
                continue
        ok, sample = model_answers(model)
        attempts.append({"model": model, "pulled": True, "answers": ok, "sample": sample})
        if ok:
            Log.ok("model %s is ready (test reply: %r)" % (model, sample))
            return {
                "ai_model": model,
                "ai_model_fallbacks": [m for m in candidates if m != model],
                "attempts": attempts,
                "num_ctx": (sizing.get("ollama") or {}).get("num_ctx", 4096),
                "keep_alive": (sizing.get("ollama") or {}).get("keep_alive", "5m"),
            }
        Log.warn("model %s downloaded but did not answer a test prompt (%s); trying the next fallback" % (model, sample))

    raise InstallError(
        "none of the configured models for profile %s could be installed and verified.\nAttempts: %s"
        % (profile, json.dumps(attempts, indent=2))
    )


# --------------------------------------------------------------------------
# Phase 5 - agent backend
# --------------------------------------------------------------------------
# Code directories are always refreshed from the repository.
CODE_DIRS = ["agent", "tools", "ui"]
# Configuration directories are operator owned: an existing file is never
# overwritten, so a tightened permission policy survives a re-run.
CONFIG_DIRS = ["policies", "config"]


def _sync_payload() -> None:
    os.makedirs(INSTALL_ROOT, exist_ok=True)
    for name in CODE_DIRS:
        source = os.path.join(REPO_ROOT, name)
        target = os.path.join(INSTALL_ROOT, name)
        if not os.path.isdir(source):
            raise InstallError("missing source directory %s" % source)
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in CONFIG_DIRS:
        source = os.path.join(REPO_ROOT, name)
        target = os.path.join(INSTALL_ROOT, name)
        os.makedirs(target, exist_ok=True)
        for entry in os.listdir(source):
            destination = os.path.join(target, entry)
            if os.path.exists(destination):
                Log.debug("keeping existing %s (operator owned)" % destination)
                continue
            shutil.copy2(os.path.join(source, entry), destination)
    # minyaml is the PyYAML-free fallback the policy loader uses.
    installer_target = os.path.join(INSTALL_ROOT, "installer")
    os.makedirs(installer_target, exist_ok=True)
    for name in ("__init__.py", "minyaml.py"):
        shutil.copy2(os.path.join(REPO_ROOT, "installer", name), os.path.join(installer_target, name))
    shutil.copy2(os.path.join(REPO_ROOT, "requirements.txt"), os.path.join(INSTALL_ROOT, "requirements.txt"))


def _build_venv() -> str:
    python = os.path.join(VENV, "bin", "python")
    if not os.path.exists(python):
        Log.step("creating the Python virtual environment")
        res = run([sys.executable, "-m", "venv", VENV], timeout=300)
        if not res.ok:
            raise InstallError("could not create a virtualenv at %s:\n%s" % (VENV, res.tail(15)))
    Log.step("installing Python dependencies")
    upgrade = run([python, "-m", "pip", "install", "--upgrade", "pip", "--quiet"], timeout=600)
    if not upgrade.ok:
        Log.warn("pip self-upgrade failed; continuing with the bundled pip")
    install = run(
        [python, "-m", "pip", "install", "--no-cache-dir", "-r", os.path.join(INSTALL_ROOT, "requirements.txt")],
        timeout=1800,
    )
    if not install.ok:
        raise InstallError("pip could not install the agent dependencies:\n%s" % install.tail(25))
    check = run([python, "-c", "import fastapi, uvicorn, pydantic, yaml; print('deps ok')"], timeout=120)
    if not check.ok:
        raise InstallError("the virtualenv is missing dependencies after install:\n%s" % check.tail(10))
    return python


def _random_password(length: int = 18) -> str:
    alphabet = string_module.ascii_letters + string_module.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _existing_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if os.path.exists(AGENT_ENV_FILE):
        try:
            for line in open(AGENT_ENV_FILE, encoding="utf-8"):
                if "=" in line and not line.strip().startswith("#"):
                    key, _, value = line.strip().partition("=")
                    values[key] = value
        except OSError:
            pass
    return values


def _write_env(settings: Dict[str, str]) -> None:
    body = [
        "# Generated by install.py. Root readable only - it holds credentials.",
        "# Change a value here and run: sudo systemctl restart %s" % AGENT_SERVICE,
    ]
    for key in sorted(settings):
        body.append("%s=%s" % (key, settings[key]))
    write_file(AGENT_ENV_FILE, "\n".join(body) + "\n", mode=0o600)


SYSTEMD_UNIT = """[Unit]
Description=AWX AI Agent backend (Smart AWX + AI Agent)
Documentation=file://{install_root}/README.md
After=network-online.target ollama.service k3s.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={env_file}
WorkingDirectory={install_root}
ExecStart={venv}/bin/uvicorn agent.api:app --host 0.0.0.0 --port {port} --workers 1 --no-access-log
Restart=always
RestartSec=5
# Self-healing: give up only if it fails 5 times inside 5 minutes.
StartLimitIntervalSec=300
StartLimitBurst=5
MemoryMax={memory_max}M
StandardOutput=append:{log_dir}/agent.log
StandardError=append:{log_dir}/agent.log
# The agent manages systemd units and packages, so it runs as root; every
# action it can take is still constrained by policies/permissions.yaml.
PrivateTmp=true
ProtectHome=read-only
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
"""


def agent_healthy(timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % AGENT_PORT, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def agent_health_payload() -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % AGENT_PORT, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def deploy_backend(
    profile: str,
    config: Dict[str, Any],
    model_info: Dict[str, Any],
    awx_url: str,
    awx_password: Optional[str],
) -> Dict[str, Any]:
    if common.DRY_RUN:
        Log.info("dry run: would copy agent/, tools/ and ui/ to %s, build a venv, write %s (0600) and the %s systemd unit" % (INSTALL_ROOT, AGENT_ENV_FILE, AGENT_SERVICE))
        return {"agent_port": AGENT_PORT, "agent_local_user": "aiadmin", "agent_health": "(dry-run)"}

    _sync_payload()
    python = _build_venv()

    existing = _existing_env()
    local_user = existing.get("AGENT_LOCAL_USER") or "aiadmin"
    local_password = _random_password()
    session_secret = existing.get("AGENT_SESSION_SECRET") or secrets.token_urlsafe(32)

    sys.path.insert(0, INSTALL_ROOT)
    hash_result = run(
        [
            python,
            "-c",
            "import sys; sys.path.insert(0, %r); from agent.auth import hash_password; print(hash_password(sys.argv[1]))" % INSTALL_ROOT,
            local_password,
        ],
        timeout=120,
    )
    if not hash_result.ok or not hash_result.out:
        raise InstallError("could not hash the local break-glass password: %s" % hash_result.tail(10))
    password_hash = hash_result.out

    sizing = config.get("sizing", {}).get(profile) or {}
    settings = {
        "AWX_URL": awx_url,
        "AWX_USERNAME": "admin",
        "AWX_PASSWORD": awx_password or "",
        "AWX_VERIFY_TLS": "false",
        "OLLAMA_URL": OLLAMA_URL,
        "AI_MODEL": model_info.get("ai_model", ""),
        "AI_MODEL_FALLBACKS": ",".join(model_info.get("ai_model_fallbacks") or []),
        "AI_NUM_CTX": str(model_info.get("num_ctx", 4096)),
        "AI_KEEP_ALIVE": str(model_info.get("keep_alive", "5m")),
        "AI_TIMEOUT": "180",
        "AGENT_SESSION_SECRET": session_secret,
        "AGENT_LOCAL_USER": local_user,
        "AGENT_LOCAL_PASSWORD_HASH": password_hash,
        "AGENT_POLICY_PATH": os.path.join(INSTALL_ROOT, "policies", "permissions.yaml"),
        "AGENT_UI_DIR": os.path.join(INSTALL_ROOT, "ui", "ai-agent"),
        "AGENT_AUDIT_LOG": os.path.join(LOG_DIR, "audit.log"),
        "AGENT_REGISTRY_PATH": os.path.join(STATE_DIR, "agents.json"),
        "KUBECONFIG": KUBECONFIG,
        "PYTHONPATH": INSTALL_ROOT,
        "PYTHONUNBUFFERED": "1",
    }
    _write_env(settings)

    # The break-glass password is written once, root-only, and echoed in the
    # final report. It is never stored in the audit log or the agent memory.
    write_file(
        os.path.join(CONFIG_DIR, "credentials.txt"),
        "Smart AWX + AI Agent - local break-glass account\n"
        "Use this only when AWX itself is down and you still need the agent.\n\n"
        "username: %s\npassword: %s\n\n"
        "Normal sign-in uses your AWX account.\n" % (local_user, local_password),
        mode=0o600,
    )

    unit_path = "/etc/systemd/system/%s.service" % AGENT_SERVICE
    write_file(
        unit_path,
        SYSTEMD_UNIT.format(
            install_root=INSTALL_ROOT,
            env_file=AGENT_ENV_FILE,
            venv=VENV,
            port=AGENT_PORT,
            memory_max=(sizing.get("agent") or {}).get("mem_limit_mb", 512),
            log_dir=LOG_DIR,
        ),
        mode=0o644,
    )
    run(["systemctl", "daemon-reload"], timeout=60)
    run(["systemctl", "enable", AGENT_SERVICE], timeout=60)
    restart = run(["systemctl", "restart", AGENT_SERVICE], timeout=180)
    if not restart.ok:
        raise InstallError("could not start %s:\n%s" % (AGENT_SERVICE, restart.tail(20)))

    if not wait_for(agent_healthy, timeout=180, interval=5, label="agent /health"):
        logs = run(["journalctl", "-u", AGENT_SERVICE, "-n", "40", "--no-pager"], mutating=False, timeout=60)
        tail = run(["tail", "-n", "40", os.path.join(LOG_DIR, "agent.log")], mutating=False, timeout=30)
        raise InstallError("the agent backend did not become healthy:\n%s\n%s" % (logs.tail(30), tail.tail(30)))

    health = agent_health_payload() or {}
    Log.ok("AI Agent backend is healthy on port %d (%d components reported)" % (AGENT_PORT, len(health.get("components", {}))))
    return {
        "agent_port": AGENT_PORT,
        "agent_local_user": local_user,
        "agent_local_password": local_password,
        "agent_health": health.get("status"),
        "agent_components": health.get("components", {}),
    }


# --------------------------------------------------------------------------
# Kubernetes secret (spec section 34)
# --------------------------------------------------------------------------
def apply_secret(awx_url: str, awx_password: Optional[str]) -> None:
    import string as _string

    if common.DRY_RUN:
        Log.info("dry run: would store AWX credentials in the Kubernetes secret smart-awx-ai-credentials")
        return

    env = _existing_env()
    template = _string.Template(open(os.path.join(REPO_ROOT, "manifests", "secrets.yaml"), encoding="utf-8").read())
    rendered = template.safe_substitute(
        AWX_NAMESPACE=AWX_NAMESPACE,
        AWX_URL=awx_url,
        AWX_USERNAME="admin",
        AWX_PASSWORD=awx_password or "",
        AGENT_SESSION_SECRET=env.get("AGENT_SESSION_SECRET", ""),
        AGENT_LOCAL_USER=env.get("AGENT_LOCAL_USER", "aiadmin"),
        AGENT_LOCAL_PASSWORD_HASH=env.get("AGENT_LOCAL_PASSWORD_HASH", ""),
    )
    applied = run(
        (["kubectl"] if which("kubectl") else ["k3s", "kubectl"]) + ["apply", "-f", "-"],
        env={"KUBECONFIG": KUBECONFIG},
        input_text=rendered,
        timeout=120,
    )
    if not applied.ok:
        Log.warn("could not store credentials as a Kubernetes secret: %s" % applied.tail(10))


# --------------------------------------------------------------------------
# Phase 6/7 - UI proxy and AWX dashboard integration
# --------------------------------------------------------------------------
def awx_service_target() -> Tuple[str, int]:
    data = kubectl_json(["get", "svc", "-n", AWX_NAMESPACE], timeout=60) or {}
    for item in data.get("items") or []:
        name = item["metadata"]["name"]
        if name in ("awx-service", "awx"):
            port = (item.get("spec", {}).get("ports") or [{}])[0].get("port", 80)
            return "%s.%s.svc.cluster.local" % (name, AWX_NAMESPACE), int(port)
    return "awx-service.%s.svc.cluster.local" % AWX_NAMESPACE, 80


def proxy_ready() -> bool:
    data = kubectl_json(["get", "deployment", "awx-ai-proxy", "-n", AWX_NAMESPACE], timeout=60)
    return bool(data) and (data.get("status", {}).get("readyReplicas") or 0) >= 1


def deploy_proxy(proxy_nodeport: int, host_ip: Optional[str] = None) -> Dict[str, Any]:
    import hashlib
    import string as _string

    host_ip = host_ip or primary_ip()
    if common.DRY_RUN:
        Log.info("dry run: would deploy the nginx AI proxy on NodePort %d, pointing at the agent on %s:%d" % (proxy_nodeport, host_ip, AGENT_PORT))
        Log.info("dry run: the proxy injects <script src=/ai-agent-inject.js> into the AWX page; the AWX image itself is not modified")
        return {"proxy_nodeport": proxy_nodeport, "host_ip": host_ip, "integration": {}}
    service_host, service_port = awx_service_target()

    nginx_conf = _string.Template(open(os.path.join(REPO_ROOT, "manifests", "proxy-nginx.conf"), encoding="utf-8").read()).safe_substitute(
        HOST_IP=host_ip,
        AGENT_PORT=AGENT_PORT,
        AWX_SERVICE=service_host,
        AWX_SERVICE_PORT=service_port,
    )
    inject_js = open(os.path.join(REPO_ROOT, "ui", "ai-agent", "inject.js"), encoding="utf-8").read()

    work_dir = os.path.join(INSTALL_ROOT, "proxy")
    os.makedirs(work_dir, exist_ok=True)
    write_file(os.path.join(work_dir, "nginx.conf"), nginx_conf)
    write_file(os.path.join(work_dir, "inject.js"), inject_js)

    kubectl_bin = ["kubectl"] if which("kubectl") else ["k3s", "kubectl"]
    rendered = run(
        kubectl_bin
        + [
            "create",
            "configmap",
            "awx-ai-proxy-config",
            "-n",
            AWX_NAMESPACE,
            "--from-file=nginx.conf=%s" % os.path.join(work_dir, "nginx.conf"),
            "--from-file=inject.js=%s" % os.path.join(work_dir, "inject.js"),
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        env={"KUBECONFIG": KUBECONFIG},
        timeout=60,
        mutating=False,
    )
    if not rendered.ok:
        raise InstallError("could not render the proxy ConfigMap:\n%s" % rendered.tail(10))
    applied = run(kubectl_bin + ["apply", "-f", "-"], env={"KUBECONFIG": KUBECONFIG}, input_text=rendered.stdout, timeout=120)
    if not applied.ok:
        raise InstallError("could not apply the proxy ConfigMap:\n%s" % applied.tail(10))

    config_hash = hashlib.sha256((nginx_conf + inject_js).encode()).hexdigest()[:16]
    manifest = _string.Template(open(os.path.join(REPO_ROOT, "manifests", "ai-agent.yaml"), encoding="utf-8").read()).safe_substitute(
        AWX_NAMESPACE=AWX_NAMESPACE,
        PROXY_NODEPORT=proxy_nodeport,
        PROXY_IMAGE=PROXY_IMAGE,
        CONFIG_HASH=config_hash,
    )
    manifest_path = os.path.join(work_dir, "ai-agent.yaml")
    write_file(manifest_path, manifest)
    res = kubectl(["apply", "-f", manifest_path], timeout=300)
    if not res.ok:
        raise InstallError("could not apply the AI proxy manifest:\n%s" % res.tail(20))

    if not wait_for(proxy_ready, timeout=420, interval=10, label="AI proxy rollout"):
        describe = kubectl(["describe", "deployment", "awx-ai-proxy", "-n", AWX_NAMESPACE], mutating=False, timeout=60)
        raise InstallError("the AI proxy never became ready:\n%s" % describe.tail(30))

    checks = verify_integration(host_ip, proxy_nodeport)
    if not checks["inject_script_served"]:
        raise InstallError("the proxy is running but does not serve /ai-agent-inject.js")
    if not checks["awx_html_contains_injection"]:
        Log.warn(
            "AWX HTML is reachable through the proxy but the injected script tag was not found. "
            "The AI Agent is still reachable at /ai-agent/ and through the floating launcher."
        )
    Log.ok("AI proxy is serving AWX and the AI Agent on port %d" % proxy_nodeport)
    return {
        "proxy_nodeport": proxy_nodeport,
        "host_ip": host_ip,
        "awx_upstream": "%s:%d" % (service_host, service_port),
        "integration": checks,
    }


def verify_integration(host_ip: str, proxy_nodeport: int) -> Dict[str, Any]:
    base = "http://127.0.0.1:%d" % proxy_nodeport
    inject = run(["curl", "-sS", "--max-time", "15", base + "/ai-agent-inject.js"], mutating=False, timeout=30)
    html = run(["curl", "-sS", "--max-time", "20", base + "/"], mutating=False, timeout=40)
    ui = run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", base + "/ai-agent/"], mutating=False, timeout=40)
    api = run(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", base + "/api/ai/session"], mutating=False, timeout=40)
    return {
        "inject_script_served": inject.ok and "awx-ai-agent-panel" in inject.stdout,
        "awx_reachable_through_proxy": html.ok and len(html.stdout) > 200,
        "awx_html_contains_injection": "ai-agent-inject.js" in (html.stdout or ""),
        "agent_ui_status": ui.out,
        # 401 is the correct answer here: the API refuses unauthenticated calls.
        "agent_api_requires_auth": api.out == "401",
    }


# --------------------------------------------------------------------------
# Firewall
# --------------------------------------------------------------------------
def configure_firewall(ports: List[int], keep_ufw: bool) -> Dict[str, Any]:
    if common.DRY_RUN:
        Log.info("dry run: would open ufw ports %s (unless --keep-ufw)" % ports)
        return {"ufw": "(dry-run)", "changed": False}
    if not which("ufw"):
        return {"ufw": "not installed", "changed": False}
    status = run(["ufw", "status"], mutating=False, timeout=30)
    active = status.ok and "Status: active" in status.stdout
    if not active:
        return {"ufw": "inactive", "changed": False}
    if keep_ufw:
        Log.warn("ufw is active and --keep-ufw was given: ports %s were NOT opened" % ports)
        return {"ufw": "active", "changed": False, "note": "left untouched by --keep-ufw"}
    opened = []
    for port in ports:
        res = run(["ufw", "allow", "%d/tcp" % port], timeout=60)
        if res.ok:
            opened.append(port)
    Log.step("opened ufw ports: %s" % opened)
    return {"ufw": "active", "changed": bool(opened), "opened": opened}


# --------------------------------------------------------------------------
# Diagnostics / recovery
# --------------------------------------------------------------------------
def diagnostics() -> str:
    parts = []
    for label, cmd in (
        ("systemctl status ollama", ["systemctl", "status", "ollama", "--no-pager", "-l"]),
        ("systemctl status agent", ["systemctl", "status", AGENT_SERVICE, "--no-pager", "-l"]),
        ("journalctl agent", ["journalctl", "-u", AGENT_SERVICE, "-n", "40", "--no-pager"]),
        ("agent log", ["tail", "-n", "40", os.path.join(LOG_DIR, "agent.log")]),
        ("ollama list", ["ollama", "list"]),
    ):
        res = run(cmd, mutating=False, timeout=60)
        parts.append("### %s\n%s" % (label, res.tail(30)))
    logs = kubectl(["logs", "-n", AWX_NAMESPACE, "deployment/awx-ai-proxy", "--tail", "40"], mutating=False, timeout=60)
    parts.append("### proxy logs\n%s" % logs.tail(30))
    return "\n\n".join(parts)


def recover(attempt: int) -> None:
    Log.step("recovery attempt %d: restarting the AI runtime and agent" % attempt)
    run(["systemctl", "restart", "ollama"], timeout=180)
    wait_for(ollama_reachable, timeout=120, interval=5, label="ollama after restart")
    run(["systemctl", "restart", AGENT_SERVICE], timeout=180)
    wait_for(agent_healthy, timeout=120, interval=5, label="agent after restart")
