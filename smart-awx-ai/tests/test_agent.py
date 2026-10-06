"""Agent runtime: grounding, approvals, verification, degradation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tools(tools_loaded):
    return tools_loaded


@pytest.fixture
def conversation():
    from agent.memory import store

    return store.get_or_create(None, "tester")


@pytest.fixture
def runtime():
    from agent.agent import Agent

    return Agent()


# ------------------------------------------------------------- grounding
def test_answer_comes_from_the_tool_not_the_model(runtime, conversation):
    """With no LLM reachable the answer must still contain real numbers."""
    result = runtime.handle("What is my RAM?", conversation)
    assert result.tool_calls and result.tool_calls[0]["tool"] == "get_memory_info"
    assert result.grounded
    assert result.degraded, "no model is running in the test environment"

    with open("/proc/meminfo", encoding="utf-8") as handle:
        total_mb = round(int(next(l for l in handle if l.startswith("MemTotal")).split()[1]) / 1024)
    assert str(total_mb) in result.answer


def test_the_model_is_given_tool_output_as_the_only_source(runtime, conversation, monkeypatch):
    from agent import agent as agent_module
    from agent.llm import LLMReply

    captured = {}

    class FakeClient:
        def chat(self, messages, **_kwargs):
            captured["messages"] = messages
            return LLMReply(text="RAM: 8 GB total, 2 GB used.", model="fake", duration_ms=5)

    monkeypatch.setattr(agent_module, "llm_client", lambda: FakeClient())
    result = runtime.handle("What is my RAM?", conversation)

    system_prompt = captured["messages"][0]["content"]
    user_prompt = captured["messages"][1]["content"]
    assert "Never add a value from your" in system_prompt
    assert "only source of truth" in user_prompt
    assert "TOOL get_memory_info" in user_prompt
    assert result.degraded is False
    assert result.model == "fake"


def test_tool_evidence_is_attached_to_the_response(runtime, conversation):
    result = runtime.handle("Show CPU usage", conversation)
    call = result.tool_calls[0]
    assert call["evidence"], "the UI must be able to show what produced the answer"
    assert any("cpuinfo" in row["command"] or "lscpu" in row["command"] for row in call["evidence"])


# ------------------------------------------------------------- approvals
def test_write_action_stops_and_asks(runtime, conversation):
    result = runtime.handle("Delete this Kubernetes namespace scratch", conversation)
    assert result.pending_approval is not None
    assert result.pending_approval["tool"] == "delete_kubernetes_namespace"
    assert result.pending_approval["confirmation_word"] == "CONFIRM"
    assert "approval" in result.answer.lower()
    # the destructive tool must NOT have run
    assert all(call["tool"] != "delete_kubernetes_namespace" for call in result.tool_calls)


def test_wrong_confirmation_word_does_not_execute(runtime, conversation):
    result = runtime.handle("Delete this Kubernetes namespace scratch", conversation)
    token = result.pending_approval["token"]
    outcome = runtime.execute_approved(token, "yes please", conversation)
    assert outcome.intent == "approval_rejected"
    assert "CONFIRM" in outcome.answer


def test_medium_risk_install_requires_approval(runtime, conversation):
    result = runtime.handle("Nginx install karo", conversation)
    assert result.pending_approval["tool"] == "install_package"
    assert result.pending_approval["risk"] == "MEDIUM RISK"
    assert all(call["tool"] != "install_package" for call in result.tool_calls)


def test_safe_write_runs_without_asking(runtime, conversation, monkeypatch):
    """restart_service on an allow-listed unit is policy-approved."""
    from tools import registry

    executed = {}

    def fake_restart(name):
        executed["name"] = name
        return (
            {"unit": name, "restarted": True, "state_before": "failed", "state_after": "active", "verified_healthy": True, "recent_log": ""},
            [{"command": "systemctl restart %s" % name, "exit_code": 0, "stdout": "", "stderr": ""}],
        )

    monkeypatch.setitem(registry.all_tools(), "restart_service", registry.get("restart_service"))
    monkeypatch.setattr(registry.get("restart_service"), "handler", fake_restart)
    result = runtime.handle("restart nginx", conversation)
    assert result.pending_approval is None
    assert executed["name"] == "nginx"


def test_approved_action_is_verified_afterwards(runtime, conversation, monkeypatch):
    """Spec section 19: EXECUTE -> VERIFY, never "executed therefore fine"."""
    from agent.permissions import approvals, evaluate
    from tools import registry

    calls = []
    original = registry.execute

    def tracking_execute(name, args=None):
        calls.append(name)
        return original(name, args)

    monkeypatch.setattr(registry, "execute", tracking_execute)
    decision = evaluate("install_package", {"name": "nginx"})
    approval = approvals.create(decision, {"name": "nginx"}, conversation.id)
    runtime.execute_approved(approval.token, None, conversation)
    assert calls[0] == "install_package"
    assert "check_package" in calls, "the installer must re-read the state it changed"


# ------------------------------------------------------------- diagnostics
def test_diagnosis_proposes_a_repair_instead_of_performing_it(runtime, conversation, monkeypatch):
    """Spec sections 24-25: find the cause, then ask before repairing."""
    from tools import registry

    def fake_pods(**_kwargs):
        return (
            {
                "scope": "awx",
                "count": 2,
                "healthy_count": 1,
                "unhealthy_count": 1,
                "unhealthy": [{"name": "awx-web-123", "namespace": "awx", "phase": "Running", "healthy": False, "restarts": 7, "problem_reasons": ["CrashLoopBackOff"]}],
                "pods": [],
            },
            [{"command": "kubectl get pods -n awx", "exit_code": 0, "stdout": "", "stderr": ""}],
        )

    monkeypatch.setattr(registry.get("get_kubernetes_pods"), "handler", fake_pods)
    result = runtime.handle("AWX down hai, fix karo", conversation)

    assert result.pending_approval is not None
    assert result.pending_approval["tool"] == "restart_pod"
    assert result.pending_approval["args"]["pod"] == "awx-web-123"
    assert any("Proposed repair" in note for note in result.notes)


def test_tool_call_budget_is_enforced(runtime, conversation, monkeypatch):
    from agent import agent as agent_module

    monkeypatch.setattr(agent_module, "settings", lambda: {"max_tool_calls_per_request": 2})
    result = runtime.handle("Server ko optimize karo", conversation)
    assert len(result.tool_calls) <= 2
    assert any("policy limit" in note for note in result.notes)


# ------------------------------------------------------------- memory
def test_conversation_history_is_kept_and_redacted():
    from agent.memory import store

    conversation = store.get_or_create(None, "tester")
    conversation.add("user", "my password: hunter2")
    assert "hunter2" not in conversation.messages[-1].content
    assert "********" in conversation.messages[-1].content


def test_history_is_capped():
    from agent.memory import MAX_MESSAGES, store

    conversation = store.get_or_create("cap-test", "tester")
    for index in range(MAX_MESSAGES + 15):
        conversation.add("user", "message %d" % index)
    assert len(conversation.messages) == MAX_MESSAGES


def test_audit_log_records_every_execution(runtime, conversation):
    from agent import audit

    runtime.handle("What is my RAM?", conversation)
    events = [entry["event"] for entry in audit.tail(30, conversation.id)]
    assert "plan" in events
    assert "tool_execution" in events
    assert "answer" in events
