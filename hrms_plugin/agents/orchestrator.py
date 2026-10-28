"""Autonomous Agentic Orchestrator implementing Multi-Turn ReAct Reasoning.

Enables the AI Plugin to execute complex, multi-step agentic workflows:
Think -> Act (Tool Call) -> Observe -> Reflect -> Act -> Final Answer
With Human-In-The-Loop proposal drafting and streaming Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from pydantic import BaseModel

from hrms_plugin.agents.supervisor import AgentResponse, UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ChatMessage, LLMGateway, TaskClass, ToolCall, ToolDefinition
from hrms_plugin.security.rbac import AuthContext

logger = logging.getLogger(__name__)

SUPER_AGENT_SYSTEM_PROMPT = (
    "You are the Enterprise Agentic HRMS Cognitive Supervisor.\n"
    "You possess deep knowledge of HR operations, labor regulations (India, UAE, Saudi Arabia, US), "
    "statutory payroll, and recruitment.\n\n"
    "Your operating invariants:\n"
    "1. STRICT LEGAL CITATIONS: Always cite specific statutory labor codes\n"
    "   (e.g. UAE Federal Decree-Law No. 33 of 2021, India Code on Wages 2019, EPF & MP Act 1952) "
    "when discussing compliance or payroll.\n"
    "2. MATHEMATICAL EXACTNESS: Never guess salary figures or statutory deductions. "
    "Always invoke the appropriate calculation tools.\n"
    "3. AUTONOMOUS PROBLEM SOLVING: Break down multi-part user requests into discrete tool calls. "
    "You may invoke multiple tools sequentially to resolve complex situations.\n"
    "4. HUMAN-IN-THE-LOOP SAFETY: Propose remediation patches for violations, "
    "declaring that mutations require human confirmation before execution.\n"
)

# Request context keys that replace LLM-supplied tool arguments of the same name.
TRUSTED_CONTEXT_PARAMS: Tuple[str, ...] = ("employees",)


class AgenticExecutionStep(BaseModel):
    step_number: int
    thought: Optional[str] = None
    tool_call: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None


def build_trusted_arguments(
    definition: ToolDefinition,
    llm_arguments: Dict[str, Any],
    trusted: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Overlay trusted request values onto LLM arguments for parameters the tool declares."""
    declared = definition.parameters.get("properties", {})
    arguments = dict(llm_arguments)
    overrides: List[Dict[str, Any]] = []
    for param, trusted_value in trusted.items():
        if param not in declared:
            continue
        if param in arguments and arguments[param] != trusted_value:
            overrides.append({"param": param, "llm_value": arguments[param], "trusted_value": trusted_value})
        arguments[param] = trusted_value
    return arguments, overrides


@dataclass
class _RunState:
    steps: List[AgenticExecutionStep] = field(default_factory=list)
    executed_tools: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    last_tool_data: Optional[Dict[str, Any]] = None


def _collect_from_output(output: Any, state: _RunState) -> None:
    if not isinstance(output, dict):
        return
    if output.get("statutory_citation"):
        state.citations.append(str(output["statutory_citation"]))
    if isinstance(output.get("statutory_citations"), list):
        state.citations.extend(output["statutory_citations"])
    if isinstance(output.get("citations"), list):
        for c in output["citations"]:
            if isinstance(c, dict) and "citation" in c:
                state.citations.append(c["citation"])
    if isinstance(output.get("violations"), list):
        for v in output["violations"]:
            if not isinstance(v, dict):
                continue
            if "statutory_citation" in v:
                state.citations.append(v["statutory_citation"])
            if v.get("remediation_patch"):
                state.actions.append(
                    {
                        "action": "REMEDIATE_PATCH",
                        "violation_id": v.get("violation_id", "VIOLATION-01"),
                        "patch": v["remediation_patch"],
                    }
                )
    if isinstance(output.get("suggested_actions"), list):
        state.actions.extend(output["suggested_actions"])
    if isinstance(output.get("actions"), list):
        state.actions.extend(output["actions"])


def _collect_from_text(text: str, state: _RunState) -> None:
    lowered = text.lower()
    if "decree" in lowered or "article" in lowered or "act" in lowered:
        for line in text.split("\n"):
            if any(k in line.lower() for k in ["article", "decree-law", "act", "section"]) and len(line) < 120:
                state.citations.append(line.strip(" -•*"))


class AgenticOrchestrator:
    """Super Agentic ReAct Engine coordinating multi-step tool reasoning and statutory compliance."""

    def __init__(
        self,
        llm_gateway: Optional[LLMGateway] = None,
        tool_registry: Optional[AgenticToolRegistry] = None,
        max_iterations: int = 5,
    ):
        self.llm_gateway = llm_gateway or LLMGateway()
        self.tool_registry = tool_registry or AgenticToolRegistry()
        self.max_iterations = max_iterations

    def _system_message(self, jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> ChatMessage:
        context_str = json.dumps(ctx) if ctx else "None provided"
        return ChatMessage(
            role="system",
            content=(
                f"{SUPER_AGENT_SYSTEM_PROMPT}\n"
                f"Current Jurisdiction: {jurisdiction}\n"
                f"Tenant ID: {tenant_id}\n"
                f"Context: {context_str}"
            ),
        )

    @staticmethod
    def _trusted_values(jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
        trusted: Dict[str, Any] = {"jurisdiction": jurisdiction, "tenant_id": tenant_id}
        for name in TRUSTED_CONTEXT_PARAMS:
            if name in ctx:
                trusted[name] = ctx[name]
        return trusted

    async def _execute_tool_call(
        self,
        tc: ToolCall,
        trusted: Dict[str, Any],
        state: _RunState,
        thought: Optional[str],
        iteration: int,
        auth: AuthContext,
    ) -> Dict[str, Any]:
        """Single dispatch path shared by run() and run_stream()."""
        definition = self.tool_registry.get_definition(tc.name)
        if definition is None:
            output: Dict[str, Any] = {"error": "UNKNOWN_TOOL", "tool": tc.name}
            arguments, overrides = dict(tc.arguments), []
        elif not self.tool_registry.is_permitted(tc.name, auth):
            logger.warning(
                "tool call not permitted",
                extra={"reason": "TOOL_NOT_PERMITTED", "tenant": auth.tenant_id, "sub": auth.user_id, "tool": tc.name},
            )
            output = {"error": "TOOL_NOT_PERMITTED", "tool": tc.name}
            arguments, overrides = dict(tc.arguments), []
        else:
            arguments, overrides = build_trusted_arguments(definition, tc.arguments, trusted)
            output = await self.tool_registry.execute(tc.name, arguments)
            state.executed_tools.append(tc.name)
            state.last_tool_data = output
            _collect_from_output(output, state)
        state.steps.append(
            AgenticExecutionStep(
                step_number=iteration,
                thought=thought,
                tool_call={"id": tc.id, "name": tc.name, "arguments": arguments, "overrides": overrides},
                observation=output,
            )
        )
        return output

    def _intent(self, state: _RunState) -> UserIntent:
        return self.tool_registry.intent_for(state.executed_tools[0]) if state.executed_tools else UserIntent.GENERAL_HR

    def _response(self, state: _RunState, reply_text: str, confidence: float) -> AgentResponse:
        return AgentResponse(
            intent=self._intent(state),
            routed_agent="AgenticBrain",
            reply_text=reply_text,
            confidence_score=confidence,
            structured_data=state.last_tool_data,
            statutory_citations=list(dict.fromkeys(state.citations)),
            suggested_actions=state.actions,
            thought_process=[s.thought for s in state.steps if s.thought],
            tool_calls=[s.tool_call for s in state.steps if s.tool_call],
        )

    async def run(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        *,
        auth: AuthContext,
    ) -> AgentResponse:
        """Execute autonomous ReAct agent loop until final answer is formulated."""
        tenant_id = auth.tenant_id
        ctx = context or {}
        messages: List[ChatMessage] = [self._system_message(jurisdiction, tenant_id, ctx)]
        for h in history or []:
            messages.append(ChatMessage(role=h.get("role", "user"), content=h.get("content", "")))
        messages.append(ChatMessage(role="user", content=user_message))

        tools = self.tool_registry.definitions_for(auth) or None
        trusted = self._trusted_values(jurisdiction, tenant_id, ctx)
        state = _RunState()

        for iteration in range(1, self.max_iterations + 1):
            llm_resp = await self.llm_gateway.chat(
                messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id
            )
            thought = llm_resp.thought_process or (
                f"Step {iteration}: Reasoning through available information." if llm_resp.tool_calls else None
            )
            if not llm_resp.tool_calls:
                _collect_from_text(llm_resp.content, state)
                return self._response(state, llm_resp.content, 0.98)

            for tc in llm_resp.tool_calls:
                output = await self._execute_tool_call(tc, trusted, state, thought, iteration, auth)
                messages.append(ChatMessage(role="assistant", content=llm_resp.content, tool_calls=[tc]))
                messages.append(
                    ChatMessage(role="tool", content=json.dumps(output), tool_call_id=tc.id, name=tc.name)
                )

        return self._response(
            state, f"Completed {len(state.steps)} agentic reasoning steps. Review findings above.", 0.90
        )

    async def run_stream(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        *,
        auth: AuthContext,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time SSE events for thoughts, tool dispatches, final tokens and the resolved intent."""
        tenant_id = auth.tenant_id
        ctx = context or {}
        tools = self.tool_registry.definitions_for(auth) or None
        trusted = self._trusted_values(jurisdiction, tenant_id, ctx)
        state = _RunState()
        yield {"event": "thinking", "data": f"Analyzing request in jurisdiction [{jurisdiction}]..."}

        messages: List[ChatMessage] = [
            self._system_message(jurisdiction, tenant_id, ctx),
            ChatMessage(role="user", content=user_message),
        ]
        llm_resp = await self.llm_gateway.chat(
            messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id
        )
        if llm_resp.thought_process:
            yield {"event": "thinking", "data": llm_resp.thought_process}

        if llm_resp.tool_calls:
            for tc in llm_resp.tool_calls:
                yield {"event": "tool_call", "data": {"name": tc.name, "arguments": tc.arguments}}
                output = await self._execute_tool_call(tc, trusted, state, llm_resp.thought_process, 1, auth)
                step_call = state.steps[-1].tool_call or {}
                yield {
                    "event": "tool_observation",
                    "data": {
                        "name": tc.name,
                        "arguments": step_call.get("arguments"),
                        "overrides": step_call.get("overrides"),
                        "output": output,
                    },
                }
                messages.append(ChatMessage(role="assistant", content="", tool_calls=[tc]))
                messages.append(
                    ChatMessage(role="tool", content=json.dumps(output), tool_call_id=tc.id, name=tc.name)
                )
            yield {"event": "thinking", "data": "Synthesizing verified compliant response..."}

        async for chunk in self.llm_gateway.chat_stream(messages, tenant_id=tenant_id):
            yield chunk
        yield {"event": "intent", "data": self._intent(state).value}
