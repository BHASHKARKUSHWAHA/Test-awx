"""FastAPI application for the AWX AI Agent.

Serves three things:

* ``/health`` and ``/status`` - component level health (spec section 39);
* ``/api/ai/*`` - the authenticated agent API;
* ``/ai-agent/`` - the chat UI, which the AWX proxy exposes inside the AWX
  dashboard.

Nothing here talks to a remote AI provider.  The only model endpoint used is
the local Ollama runtime (see ``agent/llm.py``).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from tools import load_all as load_tools
from tools import registry
from tools.awx import AWXClient

from . import audit, auth, factory
from .agent import Agent
from .llm import client as llm_client
from .memory import store
from .models import (
    ApprovalRequest,
    ChatRequest,
    ChatResponse,
    CreateAgentRequest,
    LoginRequest,
)
from .permissions import approvals, policy_document

VERSION = "1.0.0"
UI_DIR = os.environ.get("AGENT_UI_DIR", "/opt/smart-awx-ai/ui/ai-agent")
COOKIE_SECURE = os.environ.get("AGENT_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")

load_tools()
runtime = Agent()

app = FastAPI(
    title="AWX AI Agent",
    version=VERSION,
    description="Local-first operations agent for AWX, Kubernetes and Linux.",
    docs_url="/api/ai/docs",
    openapi_url="/api/ai/openapi.json",
)


# --------------------------------------------------------------------------
# Authentication dependency
# --------------------------------------------------------------------------
def current_identity(
    request: Request,
    ai_agent_session: Optional[str] = Cookie(default=None),
    awx_sessionid: Optional[str] = Cookie(default=None),
) -> auth.Identity:
    identity = auth.read_token(ai_agent_session)
    if identity:
        return identity
    if awx_sessionid:
        identity = auth.verify_awx_session_cookie(awx_sessionid)
        if identity:
            return identity
    header = request.headers.get("x-ai-agent-token")
    identity = auth.read_token(header) if header else None
    if identity:
        return identity
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


# --------------------------------------------------------------------------
# Health / status (spec section 39)
# --------------------------------------------------------------------------
def _component_health() -> Dict[str, Any]:
    components: Dict[str, Any] = {}

    linux_probe = registry.execute("get_memory_info", {})
    components["linux_tools"] = {
        "status": "healthy" if linux_probe.ok else "unhealthy",
        "detail": linux_probe.error or "reading /proc works",
    }

    model = llm_client().health()
    components["ai_model"] = {
        "status": "healthy" if (model["reachable"] and (model["model_ready"] or model["models_present"])) else "unhealthy",
        "detail": "runtime %s, model %s" % ("reachable" if model["reachable"] else "unreachable", model["configured_model"]),
        "models_present": model["models_present"],
        "active_model": model["active_model"],
    }

    k8s_probe = registry.execute("get_kubernetes_nodes", {})
    components["kubernetes_api"] = {
        "status": "healthy" if (k8s_probe.ok and (k8s_probe.data or {}).get("all_ready")) else "unhealthy",
        "detail": k8s_probe.error or "%s node(s) ready" % (k8s_probe.data or {}).get("count", 0),
    }

    awx_probe = registry.execute("get_awx_health", {})
    components["awx_api"] = {
        "status": "healthy" if (awx_probe.ok and (awx_probe.data or {}).get("status") == "healthy") else "unhealthy",
        "detail": awx_probe.error or "AWX %s" % (awx_probe.data or {}).get("version", "unknown"),
    }

    components["agent_backend"] = {"status": "healthy", "detail": "version %s, %d tools registered" % (VERSION, len(registry.all_tools()))}
    return components


@app.get("/health")
def health() -> JSONResponse:
    """Liveness + dependency health.  200 when the backend itself is usable."""
    components = _component_health()
    degraded = [name for name, value in components.items() if value["status"] != "healthy"]
    overall = "healthy" if not degraded else "degraded"
    payload = {
        "status": overall,
        "version": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "degraded_components": degraded,
        "components": components,
    }
    return JSONResponse(payload, status_code=200)


@app.get("/status")
def status_endpoint() -> Dict[str, Any]:
    model = llm_client().health()
    return {
        "service": "smart-awx-ai-agent",
        "version": VERSION,
        "uptime_seconds": int(time.time() - _STARTED),
        "tools": len(registry.all_tools()),
        "tool_categories": sorted({t.category for t in registry.all_tools().values()}),
        "model": {"configured": model["configured_model"], "active": model["active_model"], "reachable": model["reachable"], "available": model["models_present"]},
        "awx_url": AWXClient().base_url,
        "policy": {
            "auto_safe_write": policy_document().get("policy", {}).get("auto_safe_write"),
            "max_tool_calls_per_request": policy_document().get("policy", {}).get("max_tool_calls_per_request"),
        },
        "agents": [a["name"] for a in factory.list_agents()],
    }


_STARTED = time.time()


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
@app.post("/api/ai/login")
def login(payload: LoginRequest, response: Response) -> Dict[str, Any]:
    identity = auth.authenticate(payload.username, payload.password)
    if not identity:
        audit.write({"event": "login_failed", "user": payload.username})
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = auth.issue_token(identity)
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=auth.SESSION_TTL,
        path="/",
    )
    audit.write({"event": "login", "user": identity.username, "source": identity.source})
    return {"authenticated": True, "identity": identity.to_dict(), "token": token}


@app.post("/api/ai/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"authenticated": False}


@app.get("/api/ai/session")
def session(identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    return {"authenticated": True, "identity": identity.to_dict()}


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
@app.post("/api/ai/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, identity: auth.Identity = Depends(current_identity)) -> ChatResponse:
    conversation = store.get_or_create(payload.conversation_id, identity.username)
    result = runtime.handle(payload.message, conversation)
    return ChatResponse(**result.to_dict())


def _sse(event: Dict[str, Any]) -> str:
    return "data: %s\n\n" % json.dumps(event, default=str)


@app.post("/api/ai/chat/stream")
def chat_stream(payload: ChatRequest, identity: auth.Identity = Depends(current_identity)) -> StreamingResponse:
    """Live progress stream (spec section 31).

    Only operational status is streamed - which tool is running and what it
    found.  The model's internal reasoning is never emitted.
    """
    conversation = store.get_or_create(payload.conversation_id, identity.username)
    events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=200)

    def worker() -> None:
        try:
            runtime.handle(payload.message, conversation, emit=lambda event: events.put(event))
        except Exception as exc:  # noqa: BLE001 - reported to the client
            events.put({"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True, name="agent-chat").start()

    def stream() -> Generator[str, None, None]:
        yield _sse({"type": "status", "stage": "accepted", "message": "Request accepted", "conversation_id": conversation.id})
        while True:
            try:
                event = events.get(timeout=300)
            except queue.Empty:
                yield _sse({"type": "error", "message": "timed out waiting for the agent"})
                break
            if event is None:
                break
            yield _sse(event)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/ai/approve", response_model=ChatResponse)
def approve(payload: ApprovalRequest, identity: auth.Identity = Depends(current_identity)) -> ChatResponse:
    conversation = store.get_or_create(payload.conversation_id, identity.username)
    result = runtime.execute_approved(payload.token, payload.confirmation, conversation)
    return ChatResponse(**result.to_dict())


@app.get("/api/ai/approvals")
def pending_approvals(identity: auth.Identity = Depends(current_identity), conversation_id: Optional[str] = None) -> Dict[str, Any]:
    return {"pending": approvals.pending(conversation_id)}


# --------------------------------------------------------------------------
# Conversations (spec section 32)
# --------------------------------------------------------------------------
@app.get("/api/ai/conversations")
def conversations(identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    return {"conversations": store.list_for(identity.username)}


@app.get("/api/ai/conversations/{conversation_id}")
def conversation_detail(conversation_id: str, identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    conversation = store.get(conversation_id)
    if not conversation or conversation.user != identity.username:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation.to_dict()


@app.delete("/api/ai/conversations/{conversation_id}")
def conversation_delete(conversation_id: str, identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    conversation = store.get(conversation_id)
    if not conversation or conversation.user != identity.username:
        raise HTTPException(status_code=404, detail="conversation not found")
    store.clear(conversation_id)
    return {"deleted": True}


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------
@app.get("/api/ai/tools")
def tools_catalog(identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    return {"count": len(registry.all_tools()), "tools": registry.catalog()}


@app.get("/api/ai/audit")
def audit_tail(identity: auth.Identity = Depends(current_identity), limit: int = 50, conversation_id: Optional[str] = None) -> Dict[str, Any]:
    if not identity.is_superuser and identity.source != "local":
        raise HTTPException(status_code=403, detail="audit log requires a superuser")
    return {"entries": audit.tail(min(limit, 200), conversation_id)}


# --------------------------------------------------------------------------
# Agent factory (spec section 28)
# --------------------------------------------------------------------------
@app.get("/api/ai/agents")
def list_agents(identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    return {"agents": factory.list_agents()}


@app.post("/api/ai/agents")
def create_agent(payload: CreateAgentRequest, identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    try:
        definition = factory.create_agent(payload.name, payload.purpose, payload.categories, payload.max_level, identity.username)
    except factory.FactoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.write({"event": "agent_created", "user": identity.username, "agent": payload.name, "categories": payload.categories, "max_level": payload.max_level})
    return {"created": True, "agent": definition}


@app.delete("/api/ai/agents/{name}")
def delete_agent(name: str, identity: auth.Identity = Depends(current_identity)) -> Dict[str, Any]:
    if factory.get_agent(name) and factory.get_agent(name).builtin:
        raise HTTPException(status_code=400, detail="built-in agents cannot be deleted")
    if not factory.delete_agent(name):
        raise HTTPException(status_code=404, detail="agent not found")
    audit.write({"event": "agent_deleted", "user": identity.username, "agent": name})
    return {"deleted": True}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
if os.path.isdir(UI_DIR):

    @app.get("/ai-agent/")
    @app.get("/ai-agent")
    def ui_index() -> FileResponse:
        return FileResponse(os.path.join(UI_DIR, "index.html"))

    @app.get("/ai-agent/{filename}")
    def ui_file(filename: str) -> FileResponse:
        safe = os.path.normpath(filename).lstrip("/")
        path = os.path.join(UI_DIR, safe)
        if not os.path.abspath(path).startswith(os.path.abspath(UI_DIR)) or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path)
