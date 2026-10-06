"""Planner routing (spec sections 19, 26, 48).

The deterministic router is what keeps a 3B local model honest: the tool is
chosen by rules, so "RAM kitni hai?" cannot be answered from model memory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tools(tools_loaded):
    return tools_loaded


def tools_for(message):
    from agent import planner

    plan = planner.route(message)
    assert plan is not None, "no rule matched %r" % message
    return plan.intent, [step.tool for step in plan.steps]


# ------------------------------------------------- spec section 48 test list
@pytest.mark.parametrize(
    "message,expected_tool",
    [
        ("What is my RAM?", "get_memory_info"),
        ("Server me kitni RAM hai?", "get_memory_info"),
        ("Show CPU usage", "get_cpu_info"),
        ("CPU usage kitna hai", "get_cpu_info"),
        ("Show Kubernetes pods", "get_kubernetes_pods"),
        ("kitne pods chal rahe hain", "get_kubernetes_pods"),
        ("Show AWX status", "get_awx_health"),
        ("AWX ka status batao", "get_awx_health"),
        ("Find failed AWX jobs", "get_awx_failed_jobs"),
        ("AWX me failed jobs dikhao", "get_awx_failed_jobs"),
        ("Check server health", "get_health_report"),
        ("Server ka complete health report do", "get_health_report"),
        ("disk kitna bacha hai", "get_disk_info"),
        ("show me the logs", "get_logs"),
        ("kaun se ports open hain", "get_ports"),
        ("failed services dikhao", "get_services"),
    ],
)
def test_question_maps_to_the_right_tool(message, expected_tool):
    _, tools = tools_for(message)
    assert expected_tool in tools, "%r chose %s" % (message, tools)


def test_pod_diagnosis_inspects_logs_and_events():
    """Spec test 7: "why is this pod failing" must read logs and events."""
    intent, tools = tools_for("Why is pod awx-web-7c9d4 failing?")
    assert intent == "pod_diagnosis"
    assert "describe_pod" in tools
    assert "get_kubernetes_logs" in tools
    assert "get_kubernetes_events" in tools


def test_pod_name_is_extracted_even_when_it_contains_awx():
    from agent import planner

    plan = planner.route("Why is pod awx-task-abc123 failing?")
    assert plan.steps[0].args["pod"] == "awx-task-abc123"
    assert plan.steps[0].args["namespace"] == "awx"


def test_restart_service_plan_checks_then_acts_then_verifies():
    """Spec test 8: execute only after understanding, and verify afterwards."""
    intent, tools = tools_for("Restart this failed service nginx")
    assert intent == "restart_service"
    assert tools == ["get_service_status", "restart_service", "get_service_status"]


def test_restart_ai_service_resolves_to_the_agent_unit():
    """Spec test 10: "restart AI service" must mean the agent's own unit."""
    from agent import planner

    plan = planner.route("Restart AI service")
    assert plan.steps[1].args["name"] == "smart-awx-ai-agent"


def test_delete_namespace_plan_includes_the_high_risk_step():
    """Spec test 9: destructive request must still be a planned, gated step."""
    from agent import planner

    plan = planner.route("Delete this Kubernetes namespace scratch")
    tools = [step.tool for step in plan.steps]
    assert "delete_kubernetes_namespace" in tools
    assert plan.steps[-1].args["namespace"] == "scratch"
    assert any("HIGH RISK" in note for note in plan.notes)


def test_install_request_is_planned_not_executed_blindly():
    """Spec section 19: check first, install second, verify third."""
    intent, tools = tools_for("Nginx install karo")
    assert intent == "install_package"
    assert tools == ["check_package", "install_package", "check_package"]


def test_optimise_request_only_inspects():
    """Spec section 26: "optimize the server" must not change anything."""
    from agent import planner
    from tools import registry

    plan = planner.route("Server ko optimize karo")
    for step in plan.steps:
        assert registry.get(step.tool).level.value == "read_only", "%s is not read-only" % step.tool
    assert plan.diagnostic


def test_awx_down_triggers_a_diagnosis_plan_not_a_repair():
    from agent import planner
    from tools import registry

    plan = planner.route("AWX down hai, fix karo")
    assert plan.diagnostic
    for step in plan.steps:
        assert registry.get(step.tool).level.value == "read_only"


def test_every_routed_tool_exists_in_the_registry(tools_loaded):
    from agent import planner

    messages = [
        "What is my RAM?", "Show CPU usage", "disk usage", "uptime", "load average",
        "top processes", "which ports are open", "network configuration", "firewall status",
        "failed services", "restart nginx", "nginx service status", "show logs",
        "install htop", "kubernetes pods", "kubernetes nodes", "cluster events",
        "deployments in awx", "services in awx", "cluster health", "pod logs for pod awx-web-1",
        "restart pod awx-web-1", "delete namespace scratch", "why is pod awx-web-1 failing",
        "awx status", "awx projects", "awx inventories", "awx job templates", "awx jobs",
        "awx failed jobs", "awx job 12 output", "awx job 12", "launch job template 'Demo'",
        "optimize the server", "model status", "system status", "complete health report",
    ]
    for message in messages:
        plan = planner.route(message)
        assert plan is not None, "no rule for %r" % message
        for step in plan.steps:
            assert step.tool in tools_loaded.all_tools(), "%r routed to unknown tool %s" % (message, step.tool)


def test_llm_planner_discards_invented_tools(monkeypatch):
    """A hallucinated tool name must never reach execution."""
    from agent import planner
    from agent.llm import LLMReply

    class FakeClient:
        def chat(self, *_args, **_kwargs):
            return LLMReply(
                text='{"intent":"x","goal":"g","steps":[{"description":"d","tool":"run_arbitrary_shell","args":{}},'
                     '{"description":"d2","tool":"get_memory_info","args":{}}]}',
                model="fake",
                duration_ms=1,
            )

    monkeypatch.setattr(planner, "llm_client", lambda: FakeClient())
    plan = planner.plan_with_llm("do something unusual")
    assert [step.tool for step in plan.steps] == ["get_memory_info"]
    assert any("invented" in note for note in plan.notes)


def test_fallback_plan_is_honest_about_being_a_fallback():
    from agent import planner

    plan = planner.build("mujhe kuch samajh nahi aaya xyzzy", allow_llm=False)
    assert plan.strategy == "fallback"
    assert plan.notes and "could not map" in plan.notes[0]
    assert [step.tool for step in plan.steps] == ["get_system_info", "get_health_report"]
