"""Installer logic: detection, compatibility, state/resume, diagnosis, report."""

from __future__ import annotations

import os

import pytest

from installer import compatibility, hardware, recovery
from installer.common import PHASES, State, redact


# ------------------------------------------------------------- detection
def test_detection_reads_the_real_machine(config):
    detected = hardware.detect(config)
    assert detected.os["id"], "distribution id must be detected"
    assert detected.cpu["logical_cores"] >= 1
    assert detected.memory["total_mb"] > 0
    assert detected.disk["total_gb"] > 0
    assert detected.profile in config["profiles"]
    assert "GPU" in hardware.report(detected)


def test_report_includes_every_field_the_spec_asks_for(config):
    text = hardware.report(hardware.detect(config))
    for field in ("OS", "Kernel", "Architecture", "CPU", "RAM", "Swap", "Disk", "Network", "Firewall", "GPU", "Hardware profile"):
        assert field in text


# --------------------------------------------------------- compatibility
def _fake_hardware(ram_mb=8192, disk_free=50, cores=4, os_id="ubuntu", version="22.04", systemd=True, arch="x86_64"):
    machine = hardware.Hardware(
        os={"id": os_id, "version": version, "pretty": "%s %s" % (os_id, version)},
        cpu={"logical_cores": cores, "architecture": arch, "load_1m": 0.1, "model": "test"},
        memory={"total_mb": ram_mb, "available_mb": ram_mb // 2, "used_percent": 50, "swap_total_mb": 2048},
        disk={"total_gb": 100, "free_gb": disk_free, "used_percent": 50},
        kernel={"systemd": systemd, "cgroup_v2": True, "release": "6.5.0"},
        network={"interfaces": [], "default_gateway": None, "dns_servers": []},
        firewall={"ufw_active": False},
        gpu={"present": False},
        runtimes={},
    )
    machine.profile = "TEST"
    return machine


def test_supported_machine_passes(config):
    checks = compatibility.evaluate(_fake_hardware(), config)
    statuses = {check.name: check.status for check in checks}
    assert statuses["Operating system"] == compatibility.PASS
    assert statuses["RAM"] == compatibility.PASS
    assert statuses["systemd"] == compatibility.PASS


def test_tiny_machine_is_refused(config):
    checks = compatibility.evaluate(_fake_hardware(ram_mb=2048, disk_free=5), config)
    with pytest.raises(Exception) as error:
        compatibility.enforce(checks)
    assert "RAM" in str(error.value) or "disk" in str(error.value).lower()


def test_missing_systemd_is_a_hard_failure(config):
    checks = compatibility.evaluate(_fake_hardware(systemd=False), config)
    assert any(c.name == "systemd" and c.status == compatibility.FAIL for c in checks)


def test_unsupported_distribution_is_refused(config):
    checks = compatibility.evaluate(_fake_hardware(os_id="centos", version="7"), config)
    assert any(c.name == "Operating system" and c.status == compatibility.FAIL for c in checks)


def test_force_downgrades_failures_to_warnings(config):
    checks = compatibility.evaluate(_fake_hardware(systemd=False), config, force=True)
    assert all(check.status != compatibility.FAIL for check in checks)
    compatibility.enforce(checks)  # must not raise


def test_eight_gb_machine_warns_but_proceeds(config):
    """The reference 8 GB box must install, with its limits stated."""
    machine = _fake_hardware(ram_mb=8000, disk_free=50)
    checks = compatibility.evaluate(machine, config)
    compatibility.enforce(checks)
    ram_check = next(c for c in checks if c.name == "RAM")
    assert ram_check.status in (compatibility.PASS, compatibility.WARN)


# ----------------------------------------------------------- state/resume
def test_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(str(path))
    state.mark("k3s", "complete", "ok")
    state.set_fact("profile", "TEST")

    reloaded = State.load(str(path))
    assert reloaded.is_complete("k3s")
    assert reloaded.facts["profile"] == "TEST"
    assert not reloaded.is_complete("awx")


def test_resume_skips_completed_phases(tmp_path):
    """Spec section 46: phases 1-3 complete, phase 4 failed -> retry only 4."""
    path = tmp_path / "state.json"
    state = State.load(str(path))
    for name in ("preflight", "dependencies", "k3s"):
        state.mark(name, "complete")
    state.mark("awx_operator", "failed", "boom")

    reloaded = State.load(str(path))
    pending = [name for name, _ in PHASES if not reloaded.is_complete(name)]
    assert pending[0] == "awx_operator"
    assert "k3s" not in pending


def test_state_file_is_root_only(tmp_path):
    path = tmp_path / "state.json"
    State.load(str(path)).mark("k3s", "complete")
    assert oct(os.stat(path).st_mode)[-3:] == "600"


def test_attempts_are_counted(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    for _ in range(3):
        state.mark("awx", "running")
    assert state.phases["awx"].attempts == 3


# ------------------------------------------------------------- diagnosis
@pytest.mark.parametrize(
    "error,expected",
    [
        ("failed to write: no space left on device", "disk filled up"),
        ("container was OOMKilled", "memory limit"),
        ("Back-off pulling image: ImagePullBackOff", "image could not be pulled"),
        ("dial tcp 127.0.0.1:8080: connection refused", "not up yet"),
        ("Temporary failure in name resolution", "DNS resolution failed"),
        ("Error: pull model manifest: file does not exist", "model could not be downloaded"),
    ],
)
def test_known_failures_are_diagnosed(error, expected):
    result = recovery.diagnose(error)
    assert expected.lower() in result["cause"].lower()
    assert result["suggested_action"]


def test_unknown_failure_is_not_guessed():
    result = recovery.diagnose("something nobody has ever seen")
    assert result["matched"] == ""
    assert "No known failure signature" in result["cause"]


def test_diagnosis_redacts_secrets():
    text = redact("failed with AWX_PASSWORD=hunter2")
    assert "hunter2" not in text


# ---------------------------------------------------------------- report
def test_health_report_marks_missing_components_honestly(tmp_path, config):
    from installer import health

    state = State.load(str(tmp_path / "state.json"))
    state.facts.update({"host_ip": "10.0.0.5", "profile": "TEST", "ai_model": "qwen2.5:3b-instruct-q4_K_M"})
    rows = health.collect(state, hardware.detect(config))
    by_name = {row.name: row for row in rows}

    # Nothing is installed in the test environment, so nothing may read HEALTHY.
    assert by_name["K3s"].status in (health.MISSING, health.DEGRADED)
    assert by_name["AI Backend"].status == health.DEGRADED
    assert health.overall(rows) in (health.DEGRADED, health.UNKNOWN)

    report = health.render(rows, state)
    assert "http://10.0.0.5:30080" in report
    assert "AI Agent" in report


def test_awx_manifest_renders_with_profile_sizing(config):
    from installer import awx as awx_phase

    manifest = awx_phase.render_awx_manifest("TEST", config, 30081, 30080, "local-path")
    assert "nodeport_port: 30081" in manifest
    assert config["sizing"]["TEST"]["awx"]["web_mem_limit"] in manifest
    assert "${" not in manifest, "every placeholder must be substituted"
    import yaml

    parsed = yaml.safe_load(manifest)
    assert parsed["kind"] == "AWX"
    assert parsed["spec"]["service_type"] == "nodeport"


def test_proxy_config_renders_without_placeholders():
    import string

    from installer.common import REPO_ROOT

    template = string.Template(open(os.path.join(REPO_ROOT, "manifests", "proxy-nginx.conf"), encoding="utf-8").read())
    rendered = template.safe_substitute(HOST_IP="10.0.0.5", AGENT_PORT=8900, AWX_SERVICE="awx-service.awx.svc.cluster.local", AWX_SERVICE_PORT=80)
    assert "${" not in rendered
    assert "10.0.0.5:8900" in rendered
    # nginx runtime variables must survive substitution untouched
    assert "$http_upgrade" in rendered
    assert "$proxy_add_x_forwarded_for" in rendered
    assert "sub_filter '</body>'" in rendered


def test_proxy_manifest_renders_valid_yaml():
    import string

    import yaml

    from installer.common import REPO_ROOT

    template = string.Template(open(os.path.join(REPO_ROOT, "manifests", "ai-agent.yaml"), encoding="utf-8").read())
    rendered = template.safe_substitute(AWX_NAMESPACE="awx", PROXY_NODEPORT=30080, PROXY_IMAGE="nginx:1.27-alpine", CONFIG_HASH="abc")
    documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
    kinds = {doc["kind"] for doc in documents}
    assert kinds == {"Service", "Deployment"}
    service = next(doc for doc in documents if doc["kind"] == "Service")
    assert service["spec"]["ports"][0]["nodePort"] == 30080
