"""Tool registry contract and real tool execution (spec sections 15, 16, 50)."""

from __future__ import annotations

import pytest

from tools.registry import Level, ToolError, sh


@pytest.fixture(autouse=True)
def _tools(tools_loaded):
    return tools_loaded


# --------------------------------------------------------- registry contract
def test_every_tool_declares_the_required_metadata(tools_loaded):
    for name, tool in tools_loaded.all_tools().items():
        assert tool.description and len(tool.description) > 20, "%s needs a usable description" % name
        assert tool.category in ("linux", "kubernetes", "awx", "monitoring", "networking")
        assert isinstance(tool.level, Level)
        assert 1 <= tool.timeout <= 1800
        assert tool.parameters.get("type") == "object"
        for arg, rules in (tool.parameters.get("properties") or {}).items():
            assert "type" in rules, "%s.%s has no declared type" % (name, arg)


def test_write_tools_carry_a_risk_note(tools_loaded):
    for name, tool in tools_loaded.all_tools().items():
        if tool.level is not Level.READ_ONLY:
            assert tool.risk_note, "%s is a write tool and must explain its risk" % name


def test_the_catalogue_has_the_tools_the_spec_requires(tools_loaded):
    required = {
        # section 16
        "get_system_info", "get_cpu_info", "get_memory_info", "get_disk_info",
        "get_network_info", "get_processes", "get_services", "get_ports",
        "get_uptime", "get_load", "get_kernel", "get_os_info", "get_logs",
        # section 17
        "get_kubernetes_nodes", "get_kubernetes_pods", "get_kubernetes_services",
        "get_kubernetes_deployments", "get_kubernetes_events", "get_kubernetes_logs",
        "get_kubernetes_health",
        # section 18
        "list_awx_projects", "list_awx_inventories", "list_awx_job_templates",
        "launch_job_template", "get_awx_job", "get_awx_job_output", "list_awx_jobs",
        "get_awx_health",
    }
    missing = required - set(tools_loaded.all_tools())
    assert not missing, "missing tools: %s" % sorted(missing)


# ------------------------------------------------------------- command guard
def test_arbitrary_binaries_are_refused():
    with pytest.raises(ToolError):
        sh(["bash", "-c", "echo pwned"])
    with pytest.raises(ToolError):
        sh(["/bin/sh", "-c", "id"])
    with pytest.raises(ToolError):
        sh(["rm", "-rf", "/"])


def test_allowed_binary_runs_without_a_shell():
    result = sh(["uname", "-s"])
    assert result.ok and result.out == "Linux"
    # Shell metacharacters are inert because there is no shell.
    result = sh(["echo"]) if False else sh(["uname", "-s; id"])
    assert "root" not in result.stdout


# ------------------------------------------------------------- input schema
def test_unknown_arguments_are_rejected(tools_loaded):
    result = tools_loaded.execute("get_memory_info", {"inject": "; rm -rf /"})
    assert not result.ok and "unknown argument" in result.error


def test_arguments_are_pattern_checked(tools_loaded):
    result = tools_loaded.execute("get_service_status", {"name": "nginx; rm -rf /"})
    assert not result.ok and "invalid format" in result.error


def test_enum_and_range_are_enforced(tools_loaded):
    assert not tools_loaded.execute("get_processes", {"sort_by": "disk"}).ok
    assert not tools_loaded.execute("get_processes", {"limit": 9999}).ok
    assert not tools_loaded.execute("get_logs", {"source": "everything"}).ok


def test_missing_required_argument_is_rejected(tools_loaded):
    result = tools_loaded.execute("get_kubernetes_logs", {})
    assert not result.ok and "missing required argument" in result.error


# -------------------------------------------------- real data, not guesses
def test_memory_tool_matches_proc_meminfo(tools_loaded):
    result = tools_loaded.execute("get_memory_info", {})
    assert result.ok
    with open("/proc/meminfo", encoding="utf-8") as handle:
        total_kb = int(next(line for line in handle if line.startswith("MemTotal")).split()[1])
    assert abs(result.data["total_mb"] - round(total_kb / 1024)) <= 1
    assert any("meminfo" in row["command"] for row in result.evidence)


def test_cpu_tool_reports_measured_usage(tools_loaded):
    result = tools_loaded.execute("get_cpu_info", {"sample_seconds": 0.2})
    assert result.ok
    assert 0 <= result.data["usage_percent"] <= 100
    assert result.data["logical_cores"] >= 1


def test_disk_tool_reports_the_root_filesystem(tools_loaded):
    result = tools_loaded.execute("get_disk_info", {})
    assert result.ok
    assert result.data["root"]["total_gb"] > 0


def test_every_result_carries_its_evidence(tools_loaded):
    for name in ("get_memory_info", "get_disk_info", "get_load", "get_uptime", "get_os_info"):
        result = tools_loaded.execute(name, {})
        assert result.ok, "%s failed: %s" % (name, result.error)
        assert result.evidence, "%s returned no evidence" % name
        for row in result.evidence:
            assert "command" in row


def test_a_failed_underlying_command_is_reported_as_failure(tools_loaded, monkeypatch):
    """A tool whose commands all failed must not look like a success."""
    import tools.linux as linux

    def broken(*_args, **_kwargs):
        from tools.registry import Exec

        return Exec(["ss", "-tulpnH"], 127, "", "ss is not installed on this machine", 1)

    monkeypatch.setattr(linux, "sh", broken)
    result = tools_loaded.execute("get_ports", {})
    assert not result.ok
    assert "not installed" in result.error


def test_delete_file_refuses_system_paths(tools_loaded):
    for path in ("/etc/passwd", "/usr/bin/python3", "/boot/vmlinuz"):
        result = tools_loaded.execute("delete_file", {"path": path})
        assert not result.ok and "protected" in result.error


def test_delete_file_removes_only_a_real_file(tmp_path, tools_loaded):
    target = tmp_path / "scratch.txt"
    target.write_text("disposable")
    result = tools_loaded.execute("delete_file", {"path": str(target)})
    assert result.ok and result.data["deleted"]
    assert not target.exists()


def test_namespace_deletion_refuses_protected_namespaces(tools_loaded):
    result = tools_loaded.execute("delete_kubernetes_namespace", {"namespace": "kube-system"})
    assert not result.ok and "protected_namespaces" in result.error


def test_awx_tools_fail_loudly_without_a_reachable_awx(tools_loaded):
    """Unreachable AWX must surface as an error, never as "0 jobs"."""
    result = tools_loaded.execute("list_awx_jobs", {"status": "failed"})
    assert not result.ok
    assert "AWX" in result.error or "cannot reach" in result.error
