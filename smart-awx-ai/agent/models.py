"""API and internal data models."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=64)


class ApprovalRequest(BaseModel):
    token: str = Field(min_length=8, max_length=128)
    confirmation: Optional[str] = Field(default=None, max_length=64)
    conversation_id: Optional[str] = Field(default=None, max_length=64)


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=2, max_length=48, pattern=r"[A-Za-z0-9 _\-]+")
    purpose: str = Field(min_length=3, max_length=500)
    categories: List[str] = Field(default_factory=list)
    max_level: str = Field(default="read_only")


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class ToolCallView(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    ok: bool = False
    level: str = "read_only"
    duration_ms: int = 0
    error: str = ""
    summary: str = ""
    evidence: List[Dict[str, Any]] = Field(default_factory=list)


class PlanStepView(BaseModel):
    index: int
    description: str
    tool: Optional[str] = None
    args: Dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    intent: str = "general"
    plan: List[PlanStepView] = Field(default_factory=list)
    tool_calls: List[ToolCallView] = Field(default_factory=list)
    pending_approval: Optional[Dict[str, Any]] = None
    grounded: bool = True
    model: Optional[str] = None
    degraded: bool = False
    notes: List[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    components: Dict[str, Any]
    version: str
    timestamp: str
