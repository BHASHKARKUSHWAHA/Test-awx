"""Agent runtime - plan, check permission, execute, verify, answer.

The contract this file enforces (spec sections 15, 19, 26, 30, 49):

* every factual claim is produced by a tool, and the tool output is attached to
  the response as evidence;
* the model is only allowed to *phrase* the answer, with the tool output in
  front of it and an explicit instruction that it may not add facts;
* any write action stops the run and asks for approval first;
* every execution is written to the audit log, redacted.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tools import registry
from tools.registry import Level, ToolResult

from . import audit, planner, render
from .llm import LLMUnavailable
from .llm import client as llm_client
from .memory import Conversation
from .permissions import approvals, evaluate, settings

EventSink = Optional[Callable[[Dict[str, Any]], None]]

ANSWER_SYSTEM = """You are the AWX AI Agent running on a Linux server.

You are given the real output of tools that just ran on this machine.

Absolute rules:
- Use ONLY the numbers and facts in TOOL RESULTS. Never add a value from your
  own knowledge, never estimate, never fill a gap with a plausible number.
- If a needed fact is missing or a tool failed, say exactly that.
- Never print passwords, tokens, keys or secrets, even if they appear in the data.
- Be concise and operational: short lines, concrete numbers, a clear verdict.
- Answer in the language the user used (English or Hinglish).

Format: a direct answer first, then the supporting numbers, then - only if
something is wrong - a short "Recommendation" line."""


@dataclass
class AgentResult:
    conversation_id: str
    answer: str
    intent: str
    plan: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    pending_approval: Optional[Dict[str, Any]] = None
    grounded: bool = True
    degraded: bool = False
    model: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "answer": self.answer,
            "intent": self.intent,
            "plan": self.plan,
            "tool_calls": self.tool_calls,
            "pending_approval": self.pending_approval,
            "grounded": self.grounded,
            "degraded": self.degraded,
            "model": self.model,
            "notes": self.notes,
        }


class Agent:
    """One instance serves every conversation; it holds no per-user state."""

    def __init__(self, name: str = "AI Orchestrator") -> None:
        self.name = name

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def handle(self, message: str, conversation: Conversation, emit: EventSink = None) -> AgentResult:
        request_id = uuid.uuid4().hex[:12]
        conversation.add("user", message)
        self._emit(emit, {"type": "status", "stage": "understanding", "message": "Understanding the request..."})

        plan = planner.build(message, conversation.history(), allow_llm=True)
        self._emit(emit, {"type": "plan", "plan": plan.to_dict()})
        audit.write(
            {
                "event": "plan",
                "request_id": request_id,
                "conversation_id": conversation.id,
                "user": conversation.user,
                "request": message,
                "intent": plan.intent,
                "strategy": plan.strategy,
                "steps": [{"tool": s.tool, "args": s.args} for s in plan.steps],
            }
        )

        results, pending, notes = self._run_plan(plan, conversation, request_id, emit)
        notes = list(plan.notes) + notes

        # Self-healing: when a diagnosis found something, propose the repair
        # instead of performing it silently.
        if plan.diagnostic and pending is None:
            proposal = self._propose_repair(results, conversation)
            if proposal:
                pending = proposal["approval"]
                notes.append(proposal["note"])
                self._emit(emit, {"type": "approval", "approval": pending})

        answer, degraded, model_used = self._answer(message, plan, results, conversation, emit, pending)
        conversation.add("assistant", answer, {"intent": plan.intent, "tools": [r.tool for r in results]})

        audit.write(
            {
                "event": "answer",
                "request_id": request_id,
                "conversation_id": conversation.id,
                "user": conversation.user,
                "intent": plan.intent,
                "tools_used": [r.tool for r in results],
                "degraded": degraded,
                "pending_approval": bool(pending),
            }
        )

        result = AgentResult(
            conversation_id=conversation.id,
            answer=answer,
            intent=plan.intent,
            plan=[s.to_dict() for s in plan.steps],
            tool_calls=[self._view(r) for r in results],
            pending_approval=pending,
            grounded=bool(results),
            degraded=degraded,
            model=model_used,
            notes=notes,
        )
        self._emit(emit, {"type": "final", "result": result.to_dict()})
        return result

    def execute_approved(self, token: str, confirmation: Optional[str], conversation: Conversation, emit: EventSink = None) -> AgentResult:
        """Run an action the user explicitly approved."""
        request_id = uuid.uuid4().hex[:12]
        try:
            approval = approvals.consume(token, confirmation)
        except PermissionError as exc:
            audit.write({"event": "approval_rejected", "conversation_id": conversation.id, "user": conversation.user, "reason": str(exc)})
            return AgentResult(
                conversation_id=conversation.id,
                answer="Approval refused: %s" % exc,
                intent="approval_rejected",
                grounded=False,
            )

        self._emit(emit, {"type": "status", "stage": "executing", "message": "Approved. Running %s..." % approval.tool})
        audit.write(
            {
                "event": "approval_granted",
                "request_id": request_id,
                "conversation_id": conversation.id,
                "user": conversation.user,
                "tool": approval.tool,
                "args": approval.args,
                "level": approval.level.value,
            }
        )

        result = self._execute(approval.tool, approval.args, conversation, request_id, approved=True)
        self._emit(emit, {"type": "tool", "tool": self._view(result)})

        # VERIFY: for write actions, re-read the state we just changed.
        verification = self._verify(approval.tool, approval.args, conversation, request_id)
        results = [result] + ([verification] if verification else [])
        for extra in results[1:]:
            self._emit(emit, {"type": "tool", "tool": self._view(extra)})

        fake_plan = planner.Plan(
            intent="approved_action",
            goal="Execute the approved %s action" % approval.level.value.replace("_", " "),
            steps=[planner.Step(index=1, description="Run %s" % approval.tool, tool=approval.tool, args=approval.args, status="done")],
        )
        answer, degraded, model_used = self._answer(
            "Execute %s and report the outcome" % approval.tool, fake_plan, results, conversation, emit, None
        )
        conversation.add("assistant", answer, {"intent": "approved_action", "tools": [r.tool for r in results]})

        final = AgentResult(
            conversation_id=conversation.id,
            answer=answer,
            intent="approved_action",
            plan=[s.to_dict() for s in fake_plan.steps],
            tool_calls=[self._view(r) for r in results],
            grounded=True,
            degraded=degraded,
            model=model_used,
        )
        self._emit(emit, {"type": "final", "result": final.to_dict()})
        return final

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _emit(self, emit: EventSink, event: Dict[str, Any]) -> None:
        if emit:
            try:
                emit(event)
            except Exception:  # noqa: BLE001 - a broken UI stream must not kill the run
                pass

    def _run_plan(self, plan, conversation: Conversation, request_id: str, emit: EventSink):
        results: List[ToolResult] = []
        notes: List[str] = []
        pending: Optional[Dict[str, Any]] = None
        budget = int(settings().get("max_tool_calls_per_request", 12))

        for step in plan.steps:
            if not step.tool:
                step.status = "done"
                continue
            if len(results) >= budget:
                step.status = "blocked"
                notes.append("Stopped after %d tool calls (policy limit)." % budget)
                break

            decision = evaluate(step.tool, step.args)
            if not decision.allowed:
                step.status = "blocked"
                step.note = decision.reason
                notes.append("%s was blocked: %s" % (step.tool, decision.reason))
                audit.write({"event": "tool_blocked", "request_id": request_id, "conversation_id": conversation.id, "user": conversation.user, "tool": step.tool, "args": step.args, "reason": decision.reason})
                continue

            if decision.requires_approval:
                approval = approvals.create(decision, step.args, conversation.id)
                step.status = "awaiting_approval"
                step.note = decision.reason
                pending = approval.to_dict()
                pending["step_description"] = step.description
                self._emit(emit, {"type": "approval", "approval": pending})
                audit.write({"event": "approval_requested", "request_id": request_id, "conversation_id": conversation.id, "user": conversation.user, "tool": step.tool, "args": step.args, "level": decision.level.value})
                notes.append("%s needs your approval before it can run." % step.tool)
                break

            step.status = "running"
            self._emit(emit, {"type": "step", "step": step.to_dict(), "message": step.description})
            result = self._execute(step.tool, step.args, conversation, request_id)
            step.status = "done" if result.ok else "failed"
            step.note = result.error[:200]
            results.append(result)
            self._emit(emit, {"type": "tool", "tool": self._view(result), "step_index": step.index})

        return results, pending, notes

    def _execute(self, tool_name: str, args: Dict[str, Any], conversation: Conversation, request_id: str, approved: bool = False) -> ToolResult:
        started = time.time()
        result = registry.execute(tool_name, args)
        try:
            level = registry.get(tool_name).level.value
        except Exception:  # noqa: BLE001
            level = "unknown"
        audit.write(
            {
                "event": "tool_execution",
                "request_id": request_id,
                "conversation_id": conversation.id,
                "user": conversation.user,
                "tool": tool_name,
                "args": args,
                "level": level,
                "approved": approved,
                "success": result.ok,
                "error": result.error,
                "duration_ms": int((time.time() - started) * 1000),
                "commands": [row.get("command") for row in result.evidence],
            }
        )
        return result

    def _verify(self, tool_name: str, args: Dict[str, Any], conversation: Conversation, request_id: str) -> Optional[ToolResult]:
        """Independent re-read after a write, so success is observed not assumed."""
        verifiers = {
            "restart_service": ("get_service_status", {"name": args.get("name", "")}),
            "stop_service": ("get_service_status", {"name": args.get("name", "")}),
            "install_package": ("check_package", {"name": args.get("name", "")}),
            "restart_pod": ("get_kubernetes_pods", {"namespace": args.get("namespace", "awx")}),
            "scale_deployment": ("get_kubernetes_deployments", {"namespace": args.get("namespace", "awx")}),
            "set_deployment_resources": ("get_kubernetes_pods", {"namespace": args.get("namespace", "awx")}),
            "delete_kubernetes_namespace": ("get_kubernetes_pods", {"namespace": args.get("namespace", "")}),
            "launch_job_template": ("list_awx_jobs", {"limit": 5}),
            "relaunch_job": ("list_awx_jobs", {"limit": 5}),
        }
        entry = verifiers.get(tool_name)
        if not entry:
            return None
        verify_tool, verify_args = entry
        if not all(verify_args.values()):
            return None
        return self._execute(verify_tool, verify_args, conversation, request_id)

    def _propose_repair(self, results: List[ToolResult], conversation: Conversation) -> Optional[Dict[str, Any]]:
        """Derive a repair proposal from what the diagnosis actually found.

        Deliberately rule based: a repair is only offered when the evidence
        for it is in the tool output, never because the model suggested it.
        """
        memory_pressure = False
        oom = False
        crashloop_pod: Optional[Dict[str, Any]] = None
        namespace = "awx"
        failed_job_id: Optional[int] = None

        for result in results:
            if not result.ok or not isinstance(result.data, dict):
                continue
            data = result.data
            if result.tool in ("get_memory_info", "get_resource_summary"):
                percent = data.get("used_percent") if result.tool == "get_memory_info" else data.get("memory_percent")
                memory_pressure = memory_pressure or (percent is not None and percent >= 90)
            if result.tool == "describe_pod":
                oom = oom or bool(data.get("oom_killed"))
                summary = data.get("summary") or {}
                if summary and not summary.get("healthy"):
                    crashloop_pod = {"pod": data.get("pod"), "namespace": data.get("namespace")}
            if result.tool in ("get_kubernetes_pods", "get_pod_restart_summary"):
                for pod in data.get("unhealthy") or data.get("crashloop") or []:
                    if pod.get("name"):
                        crashloop_pod = {"pod": pod["name"], "namespace": pod.get("namespace", namespace)}
                        break
            if result.tool == "get_awx_failed_jobs":
                jobs = data.get("failed_jobs") or []
                if jobs:
                    failed_job_id = jobs[0].get("id")

        if crashloop_pod and crashloop_pod.get("pod"):
            decision = evaluate("restart_pod", crashloop_pod)
            approval = approvals.create(decision, crashloop_pod, conversation.id)
            payload = approval.to_dict()
            payload["step_description"] = "Restart the unhealthy pod %s" % crashloop_pod["pod"]
            note = "Proposed repair: restart pod %s in namespace %s." % (crashloop_pod["pod"], crashloop_pod.get("namespace"))
            if oom or memory_pressure:
                note += " The evidence points at memory pressure, so a restart may only buy time - consider lowering the AI model size or raising the pod memory limit."
            return {"approval": payload, "note": note + " Approve to proceed."}

        if failed_job_id:
            decision = evaluate("relaunch_job", {"job_id": failed_job_id})
            approval = approvals.create(decision, {"job_id": failed_job_id}, conversation.id)
            payload = approval.to_dict()
            payload["step_description"] = "Re-run failed AWX job %d" % failed_job_id
            return {
                "approval": payload,
                "note": "Proposed repair: re-run AWX job %d with the same parameters. Approve to proceed." % failed_job_id,
            }
        return None

    def _answer(self, message: str, plan, results: List[ToolResult], conversation: Conversation, emit: EventSink, pending: Optional[Dict[str, Any]]):
        """Phrase the answer from tool output; fall back to deterministic text."""
        deterministic = render.plain_answer(plan.intent, results)

        if pending:
            prompt_lines = [
                "I need your approval before I continue.",
                "",
                "Action: %s" % (pending.get("step_description") or pending.get("tool")),
                "Tool: %s" % pending.get("tool"),
                "Arguments: %s" % (pending.get("args") or {}),
                "Risk: %s" % pending.get("risk"),
            ]
            if pending.get("risk_note"):
                prompt_lines.append("Why it matters: %s" % pending["risk_note"])
            if pending.get("confirmation_word"):
                prompt_lines.append('Type "%s" to confirm.' % pending["confirmation_word"])
            if results:
                prompt_lines += ["", "What I found so far:", deterministic]
            return "\n".join(prompt_lines), False, None

        if not results:
            return deterministic, True, None

        self._emit(emit, {"type": "status", "stage": "summarising", "message": "Summarising the results..."})
        facts = render.facts_block(results)
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": "USER REQUEST:\n%s\n\nTOOL RESULTS (this is the only source of truth):\n%s\n\n"
                "Write the answer now. Use only the values above." % (message[:1000], facts[:9000]),
            },
        ]
        try:
            reply = llm_client().chat(messages, temperature=0.1, max_tokens=700)
        except LLMUnavailable:
            return (
                deterministic + "\n\n(The local AI model is not reachable, so this answer is the raw tool output. "
                "Run `sudo systemctl status ollama` or `sudo python3 install.py --repair` to restore it.)",
                True,
                None,
            )
        text = reply.text.strip()
        if not text:
            return deterministic, True, reply.model
        return text, False, reply.model

    def _view(self, result: ToolResult) -> Dict[str, Any]:
        try:
            level = registry.get(result.tool).level
        except Exception:  # noqa: BLE001
            level = Level.READ_ONLY
        payload = audit.redact(
            {
                "tool": result.tool,
                "args": result.args,
                "ok": result.ok,
                "level": level.value,
                "duration_ms": result.duration_ms,
                "error": result.error[:300],
                "summary": render.summarise_tool(result),
                "evidence": [
                    {"command": row.get("command"), "exit_code": row.get("exit_code"), "stdout": (row.get("stdout") or "")[:1200]}
                    for row in result.evidence[:6]
                ],
            }
        )
        return payload


runtime = Agent()
