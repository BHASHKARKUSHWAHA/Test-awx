"""Permission engine, approvals and redaction (spec sections 22, 23, 37, 51)."""

from __future__ import annotations

import time

import pytest

from tools.registry import Level


@pytest.fixture(autouse=True)
def _tools(tools_loaded):
    return tools_loaded


def test_read_only_tools_never_need_approval():
    from agent.permissions import evaluate

    for name in ("get_memory_info", "get_cpu_info", "get_kubernetes_pods", "get_awx_health"):
        decision = evaluate(name, {})
        assert decision.allowed and not decision.requires_approval
        assert decision.level is Level.READ_ONLY


def test_high_risk_tools_always_need_typed_confirmation():
    from agent.permissions import evaluate

    decision = evaluate("delete_kubernetes_namespace", {"namespace": "scratch"})
    assert decision.level is Level.HIGH_RISK
    assert decision.requires_approval
    assert decision.confirmation_word == "CONFIRM"


def test_protected_namespace_is_flagged_before_execution():
    from agent.permissions import evaluate

    decision = evaluate("delete_kubernetes_namespace", {"namespace": "kube-system"})
    assert decision.level is Level.HIGH_RISK
    assert "protected namespace" in decision.reason


def test_restart_of_a_critical_service_escalates_to_high_risk():
    """restart_service is SAFE_WRITE, but restarting k3s is not."""
    from agent.permissions import evaluate

    safe = evaluate("restart_service", {"name": "nginx"})
    assert safe.level is Level.SAFE_WRITE and not safe.requires_approval

    unknown = evaluate("restart_service", {"name": "some-random-unit"})
    assert unknown.level is Level.MEDIUM_RISK and unknown.requires_approval

    critical = evaluate("restart_service", {"name": "k3s"})
    assert critical.level is Level.HIGH_RISK
    assert critical.requires_approval
    assert critical.escalated_from == "safe_write"


def test_scaling_to_zero_escalates():
    from agent.permissions import evaluate

    assert evaluate("scale_deployment", {"deployment": "awx-web", "replicas": 2}).level is Level.MEDIUM_RISK
    assert evaluate("scale_deployment", {"deployment": "awx-web", "replicas": 0}).level is Level.HIGH_RISK


def test_unknown_tool_is_denied():
    from agent.permissions import evaluate

    decision = evaluate("rm_minus_rf", {})
    assert not decision.allowed


def test_package_outside_the_allow_list_escalates():
    from agent.permissions import evaluate

    assert evaluate("install_package", {"name": "nginx"}).level is Level.MEDIUM_RISK
    assert evaluate("install_package", {"name": "some-unlisted-package"}).level is Level.HIGH_RISK


# --------------------------------------------------------------------- approvals
def test_approval_requires_the_exact_confirmation_word():
    from agent.permissions import approvals, evaluate

    decision = evaluate("delete_kubernetes_namespace", {"namespace": "scratch"})
    approval = approvals.create(decision, {"namespace": "scratch"}, "conv1")

    with pytest.raises(PermissionError):
        approvals.consume(approval.token, "yes")
    with pytest.raises(PermissionError):
        approvals.consume(approval.token, "confirm")  # case matters

    consumed = approvals.consume(approval.token, "CONFIRM")
    assert consumed.tool == "delete_kubernetes_namespace"


def test_an_approval_can_only_be_used_once():
    from agent.permissions import approvals, evaluate

    decision = evaluate("install_package", {"name": "nginx"})
    approval = approvals.create(decision, {"name": "nginx"}, "conv1")
    approvals.consume(approval.token)
    with pytest.raises(PermissionError):
        approvals.consume(approval.token)


def test_expired_approvals_are_rejected(monkeypatch):
    from agent import permissions

    decision = permissions.evaluate("install_package", {"name": "nginx"})
    approval = permissions.approvals.create(decision, {"name": "nginx"}, "conv1")
    approval.expires_at = time.time() - 1
    with pytest.raises(PermissionError):
        permissions.approvals.consume(approval.token)


def test_approval_token_is_bound_to_its_arguments():
    from agent.permissions import approvals, evaluate

    decision = evaluate("restart_pod", {"pod": "awx-web-1", "namespace": "awx"})
    approval = approvals.create(decision, {"pod": "awx-web-1", "namespace": "awx"}, "conv1")
    consumed = approvals.consume(approval.token)
    assert consumed.args == {"pod": "awx-web-1", "namespace": "awx"}


# --------------------------------------------------------------------- redaction
@pytest.mark.parametrize(
    "text",
    [
        "AWX_PASSWORD=hunter2",
        'password: "s3cr3t"',
        "token: abcdef0123456789abcdef",
        "api_key=AKIAIOSFODNN7EXAMPLE",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
    ],
)
def test_secrets_are_redacted(text):
    from agent.audit import redact_text

    cleaned = redact_text(text)
    assert "********" in cleaned
    for secret in ("hunter2", "s3cr3t", "abcdef0123456789abcdef", "AKIAIOSFODNN7EXAMPLE"):
        assert secret not in cleaned


def test_normal_output_is_not_mangled():
    from agent.audit import redact_text

    text = "RAM 1234/8192 MB used (15.1%), swap 0 MB"
    assert redact_text(text) == text


def test_nested_structures_are_redacted():
    from agent.audit import redact

    cleaned = redact({"password": "x", "nested": {"token": "y", "ram_mb": 8192}, "rows": ["secret=z"]})
    assert cleaned["password"] == "********"
    assert cleaned["nested"]["token"] == "********"
    assert cleaned["nested"]["ram_mb"] == 8192
    assert "z" not in cleaned["rows"][0]


def test_audit_entries_are_written_redacted(isolated_environment):
    from agent import audit

    audit.write({"event": "tool_execution", "tool": "login", "args": {"password": "hunter2"}, "user": "admin"})
    entries = audit.tail(5)
    assert entries, "audit log should contain the entry"
    assert entries[-1]["args"]["password"] == "********"
    with open(isolated_environment / "audit.log", encoding="utf-8") as handle:
        assert "hunter2" not in handle.read()
