"""HTTP surface: authentication, health, chat, approvals, agent factory."""

from __future__ import annotations

import os

import pytest

from fastapi.testclient import TestClient

LOCAL_USER = "aiadmin"
LOCAL_PASSWORD = "test-break-glass-password"


@pytest.fixture(scope="module")
def client(request):
    from agent.auth import hash_password

    os.environ["AGENT_LOCAL_USER"] = LOCAL_USER
    os.environ["AGENT_LOCAL_PASSWORD_HASH"] = hash_password(LOCAL_PASSWORD)
    from agent.api import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed(client):
    response = client.post("/api/ai/login", json={"username": LOCAL_USER, "password": LOCAL_PASSWORD})
    assert response.status_code == 200, response.text
    return client


# ------------------------------------------------------------------ auth
def test_every_agent_endpoint_requires_authentication(client):
    for method, path, payload in [
        ("get", "/api/ai/session", None),
        ("post", "/api/ai/chat", {"message": "hi"}),
        ("post", "/api/ai/chat/stream", {"message": "hi"}),
        ("get", "/api/ai/tools", None),
        ("get", "/api/ai/conversations", None),
        ("get", "/api/ai/agents", None),
        ("post", "/api/ai/approve", {"token": "x" * 10}),
    ]:
        response = getattr(client, method)(path, json=payload) if payload else getattr(client, method)(path)
        assert response.status_code == 401, "%s %s was reachable without a session" % (method, path)


def test_bad_credentials_are_rejected(client):
    response = client.post("/api/ai/login", json={"username": LOCAL_USER, "password": "wrong"})
    assert response.status_code == 401


def test_login_sets_an_httponly_cookie(client):
    response = client.post("/api/ai/login", json={"username": LOCAL_USER, "password": LOCAL_PASSWORD})
    assert response.status_code == 200
    cookie_header = response.headers.get("set-cookie", "")
    assert "ai_agent_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert response.json()["identity"]["username"] == LOCAL_USER


def test_login_response_does_not_leak_the_password(client):
    response = client.post("/api/ai/login", json={"username": LOCAL_USER, "password": LOCAL_PASSWORD})
    assert LOCAL_PASSWORD not in response.text


# ---------------------------------------------------------------- health
def test_health_lists_every_component(client):
    body = client.get("/health").json()
    assert body["status"] in ("healthy", "degraded")
    for component in ("linux_tools", "ai_model", "kubernetes_api", "awx_api", "agent_backend"):
        assert component in body["components"]
    # Linux tools work everywhere this test runs; the rest are absent here.
    assert body["components"]["linux_tools"]["status"] == "healthy"


def test_health_does_not_claim_components_it_cannot_reach(client):
    body = client.get("/health").json()
    assert body["components"]["awx_api"]["status"] == "unhealthy"
    assert body["components"]["kubernetes_api"]["status"] == "unhealthy"


def test_status_reports_the_tool_catalogue(client):
    body = client.get("/status").json()
    assert body["tools"] > 40
    assert set(body["tool_categories"]) == {"linux", "kubernetes", "awx", "monitoring", "networking"}


# ------------------------------------------------------------------ chat
def test_chat_returns_a_grounded_answer(authed):
    body = authed.post("/api/ai/chat", json={"message": "What is my RAM?"}).json()
    assert body["intent"] == "memory"
    assert body["tool_calls"][0]["tool"] == "get_memory_info"
    assert body["grounded"] is True
    assert body["conversation_id"]


def test_chat_stream_emits_plan_tool_and_final_events(authed):
    with authed.stream("POST", "/api/ai/chat/stream", json={"message": "Show CPU usage"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = "".join(response.iter_text())
    for marker in ('"type": "plan"', '"type": "tool"', '"type": "final"', "[DONE]"):
        assert marker in payload, "missing %s in the stream" % marker


def test_chat_stream_does_not_leak_model_reasoning(authed):
    with authed.stream("POST", "/api/ai/chat/stream", json={"message": "What is my RAM?"}) as response:
        payload = "".join(response.iter_text())
    for forbidden in ("chain of thought", "<think>", "reasoning:"):
        assert forbidden not in payload.lower()


def test_approval_flow_over_http(authed):
    body = authed.post("/api/ai/chat", json={"message": "Delete this Kubernetes namespace scratch"}).json()
    approval = body["pending_approval"]
    assert approval["risk"] == "HIGH RISK"

    wrong = authed.post("/api/ai/approve", json={"token": approval["token"], "confirmation": "ok", "conversation_id": body["conversation_id"]}).json()
    assert wrong["intent"] == "approval_rejected"

    # The token was not consumed by the failed attempt, so a correct
    # confirmation still works - and the tool itself refuses protected names.
    body2 = authed.post("/api/ai/chat", json={"message": "Delete this Kubernetes namespace kube-system"}).json()
    assert body2["pending_approval"]["tool"] == "delete_kubernetes_namespace"


def test_conversation_history_is_listed(authed):
    first = authed.post("/api/ai/chat", json={"message": "uptime"}).json()
    conversation_id = first["conversation_id"]
    listing = authed.get("/api/ai/conversations").json()["conversations"]
    assert any(item["id"] == conversation_id for item in listing)
    detail = authed.get("/api/ai/conversations/%s" % conversation_id).json()
    assert detail["messages"][0]["role"] == "user"


def test_tool_catalogue_is_exposed_for_inspection(authed):
    body = authed.get("/api/ai/tools").json()
    names = {tool["name"] for tool in body["tools"]}
    assert "get_memory_info" in names
    for tool in body["tools"]:
        assert tool["permission"] in ("read_only", "safe_write", "medium_risk", "high_risk")


# --------------------------------------------------------- agent factory
def test_builtin_agents_are_listed(authed):
    names = {agent["name"] for agent in authed.get("/api/ai/agents").json()["agents"]}
    assert {"Linux Agent", "Kubernetes Agent", "AWX Agent", "Network Agent", "Monitoring Agent", "Security Agent"} <= names


def test_agent_creation_respects_policy(authed):
    created = authed.post(
        "/api/ai/agents",
        json={"name": "Disk Watcher", "purpose": "watch disks", "categories": ["linux", "monitoring"], "max_level": "read_only"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["agent"]["tool_count"] > 0

    too_powerful = authed.post(
        "/api/ai/agents",
        json={"name": "Root Agent", "purpose": "do anything", "categories": ["linux"], "max_level": "high_risk"},
    )
    assert too_powerful.status_code == 400
    assert "caps created agents" in too_powerful.json()["detail"]


def test_builtin_agents_cannot_be_deleted(authed):
    response = authed.delete("/api/ai/agents/Linux Agent")
    assert response.status_code == 400


# ------------------------------------------------------------------- UI
def test_the_chat_ui_is_served(client):
    """The UI must be real files served by the backend, not a placeholder."""
    index = client.get("/ai-agent/")
    assert index.status_code == 200
    assert "AWX AI Agent" in index.text
    assert 'src="app.js"' in index.text

    for asset, marker in (("app.js", "/chat/stream"), ("style.css", "--accent"), ("inject.js", "awx-ai-agent-panel")):
        response = client.get("/ai-agent/%s" % asset)
        assert response.status_code == 200, asset
        assert marker in response.text, "%s does not look like the real file" % asset


def test_ui_path_traversal_is_blocked(client):
    for path in ("/ai-agent/../../etc/passwd", "/ai-agent/%2e%2e/%2e%2e/etc/passwd"):
        assert client.get(path).status_code == 404


def test_injected_script_targets_the_agent_ui(client):
    """The AWX dashboard entry point must open the real agent, not a stub."""
    body = client.get("/ai-agent/inject.js").text
    assert '/ai-agent/' in body
    assert "MutationObserver" in body, "the nav item must survive AWX SPA re-renders"
    assert "altKey" in body, "Alt+A shortcut"
