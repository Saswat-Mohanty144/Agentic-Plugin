"""Autonomous Agentic Orchestrator implementing Multi-Turn ReAct Reasoning.

Enables the AI Plugin to execute complex, multi-step agentic workflows:
Think -> Act (Tool Call) -> Observe -> Reflect -> Act -> Final Answer
With Human-In-The-Loop proposal drafting and streaming Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel

from hrms_plugin.agents.supervisor import AgentResponse, UserIntent, matching_intents
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import LENGTH_STOP_REASONS, ChatMessage, LLMGateway, TaskClass, ToolCall, ToolDefinition
from hrms_plugin.security.pii import PiiSession
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

# Context keys whose VALUES may be shown to the LLM (navigation only). Every other context value reaches
# tools by trusted server-side injection and is never put in a prompt (piece C, decision C3).
LLM_VISIBLE_CONTEXT_KEYS: Tuple[str, ...] = ("entity", "record_id", "route")
# Values that only the server may set; context entries with these names are ignored.
SERVER_OWNED_PARAMS: Tuple[str, ...] = ("jurisdiction", "tenant_id")
# Tool results above this size are not sent to the LLM; the UI still receives them (piece H5).
MAX_TOOL_RESULT_CHARS = 20000


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def stated_numbers(texts: Iterable[str]) -> Set[Decimal]:
    """Numbers as the user wrote them. Digit groupings (1,20,000 / 120,000.00) normalise; word forms do not."""
    found: Set[Decimal] = set()
    for text in texts:
        for match in _NUMBER.finditer(text or ""):
            try:
                found.add(Decimal(match.group(0).replace(",", "")))
            except InvalidOperation:
                continue
    return found


def _context_numbers(value: Any) -> Set[Decimal]:
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float, Decimal)):
        return {Decimal(str(value))}
    if isinstance(value, str):
        return stated_numbers([value])
    if isinstance(value, dict):
        return set().union(*(_context_numbers(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_context_numbers(v) for v in value)) if value else set()
    return set()


def _grounded(value: Any, stated: Set[Decimal]) -> bool:
    """Every number inside ``value`` (and every string that is wholly a number) must have been stated."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return Decimal(str(value)) in stated
    if isinstance(value, str):
        return not _NUMBER.fullmatch(value.strip()) or Decimal(value.strip().replace(",", "")) in stated
    if isinstance(value, dict):
        return all(_grounded(v, stated) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_grounded(v, stated) for v in value)
    return True


def _user_numbers(user_message: str, history: Optional[List[Dict[str, str]]], ctx: Dict[str, Any]) -> Set[Decimal]:
    """What may ground a calculation: the user's own turns and host-supplied context, never assistant text."""
    texts = [user_message] + [h.get("content", "") for h in history or [] if h.get("role") == "user"]
    return stated_numbers(texts) | _context_numbers(ctx)


def tool_result_content(output: Any) -> str:
    """Compact JSON for the LLM; an oversized result becomes an explicit error instead of silent truncation."""
    text = json.dumps(output, default=str, separators=(",", ":"))
    if len(text) > MAX_TOOL_RESULT_CHARS:
        replacement: Dict[str, Any] = {"error": "TOOL_OUTPUT_TOO_LARGE", "chars": len(text)}
        code = output.get("error") if isinstance(output, dict) else None
        if isinstance(code, str) and len(code) <= 100:
            replacement["tool_error"] = code  # keep the tool's own error code visible (agy H-F8)
        return json.dumps(replacement, separators=(",", ":"))
    return text


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
    pii: PiiSession = field(default_factory=PiiSession)
    stated: Set[Decimal] = field(default_factory=set)


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
                        "entity_id": v.get("entity_id") or v.get("employee_id"),
                        "entity_type": v.get("entity_type"),
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


def _result_data(state: _RunState, *, partial: bool = False, truncated: bool = False) -> Dict[str, Any]:
    return {
        "statutory_citations": list(dict.fromkeys(state.citations)),
        "suggested_actions": state.actions,
        "partial": partial,
        "truncated": truncated,
    }


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

    def _system_messages(self, jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> List[ChatMessage]:
        """Frozen prompt first (cacheable prefix, piece H1), then this request's jurisdiction, tenant and context."""
        keys = sorted(k for k in ctx if k not in SERVER_OWNED_PARAMS)
        navigation = [f"{k}={ctx[k]}" for k in LLM_VISIBLE_CONTEXT_KEYS if k in ctx]
        context_str = (
            "Context fields provided by the host application (values are passed to tools automatically): "
            + (", ".join(keys) if keys else "none")
            + ("; navigation: " + ", ".join(navigation) if navigation else "")
        )
        return [
            ChatMessage(role="system", content=SUPER_AGENT_SYSTEM_PROMPT),
            ChatMessage(
                role="system",
                content=f"Current Jurisdiction: {jurisdiction}\nTenant ID: {tenant_id}\nContext: {context_str}",
            ),
        ]

    @staticmethod
    def _trusted_values(jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
        trusted: Dict[str, Any] = {k: v for k, v in ctx.items() if k not in SERVER_OWNED_PARAMS}
        trusted.update({"jurisdiction": jurisdiction, "tenant_id": tenant_id})
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
        # Learn identifiers from the call itself: a refused or failed call produces no output to learn from, yet its
        # arguments are echoed back to the LLM in the assistant turn (QA follow-up).
        state.pii.learn(tc.arguments)
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
            ungrounded = [p for p in self.tool_registry.user_stated_params(tc.name)
                          if p in arguments and not _grounded(arguments[p], state.stated)]
            if ungrounded:
                logger.warning("tool call refused: ungrounded numbers",
                               extra={"reason": "UNGROUNDED_AMOUNT", "tenant": auth.tenant_id, "tool": tc.name})
                output = {"success": False, "error": "UNGROUNDED_AMOUNT", "params": ungrounded}
            else:
                output = await self.tool_registry.execute(tc.name, arguments)
            state.pii.learn(output)
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

    def _offered_tools(self, auth: AuthContext, user_message: str,
                       history: Optional[List[Dict[str, str]]]) -> Optional[List[ToolDefinition]]:
        """QE-2: only the tools for the intents in the user's turns, plus the regulation search; anything unmatched,
        or a match with no permitted tool, offers every permitted tool. Permissions are never widened."""
        permitted = self.tool_registry.definitions_for(auth)
        texts = [user_message] + [h.get("content", "") for h in history or [] if h.get("role") == "user"]
        intents = set().union(*(matching_intents(t) for t in texts))
        if intents:
            chosen = [d for d in permitted if self.tool_registry.intent_for(d.name) in intents]
            if chosen:
                return [d for d in permitted if d in chosen or d.name == "search_labor_regulations"]
        return permitted or None

    @staticmethod
    def _assistant_turn(llm_resp: Any) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=llm_resp.content,
            tool_calls=list(llm_resp.tool_calls),
            raw_content=llm_resp.raw_content,
        )

    def _intent(self, state: _RunState) -> UserIntent:
        return self.tool_registry.intent_for(state.executed_tools[0]) if state.executed_tools else UserIntent.GENERAL_HR

    def _response(self, state: _RunState, reply_text: str, confidence: float) -> AgentResponse:
        data = _result_data(state)
        return AgentResponse(
            intent=self._intent(state),
            routed_agent="AgenticBrain",
            reply_text=reply_text,
            confidence_score=confidence,
            structured_data=state.last_tool_data,
            statutory_citations=data["statutory_citations"],
            suggested_actions=data["suggested_actions"],
            thought_process=[s.thought for s in state.steps if s.thought],
            tool_calls=[s.tool_call for s in state.steps if s.tool_call],
        )

    def _prepare_session(
        self,
        user_message: str,
        jurisdiction: str,
        context: Optional[Dict[str, Any]],
        history: Optional[List[Dict[str, str]]],
        auth: AuthContext,
    ) -> Tuple[str, _RunState, List[ChatMessage], Optional[List[ToolDefinition]], Dict[str, Any]]:
        tenant_id = auth.tenant_id
        ctx = context or {}
        policy = self.llm_gateway.preflight(tenant_id)
        state = _RunState(
            pii=PiiSession(tokenize_compensation=policy.tokenize_compensation),
            stated=_user_numbers(user_message, history, ctx),
        )
        state.pii.learn(ctx)
        messages: List[ChatMessage] = self._system_messages(jurisdiction, tenant_id, ctx)
        for h in history or []:
            messages.append(ChatMessage(role=h.get("role", "user"), content=h.get("content", "")))
        messages.append(ChatMessage(role="user", content=user_message))
        tools = self._offered_tools(auth, user_message, history)
        trusted = self._trusted_values(jurisdiction, tenant_id, ctx)
        return tenant_id, state, messages, tools, trusted

    def _tool_message(self, tc: ToolCall, output: Any) -> ChatMessage:
        return ChatMessage(
            role="tool",
            content=tool_result_content(self.tool_registry.model_view(tc.name, output)),
            tool_call_id=tc.id,
            name=tc.name,
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
        tenant_id, state, messages, tools, trusted = self._prepare_session(
            user_message, jurisdiction, context, history, auth
        )

        for iteration in range(1, self.max_iterations + 1):
            llm_resp = await self.llm_gateway.chat(
                messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id, pii=state.pii
            )
            thought = llm_resp.thought_process or (
                f"Step {iteration}: Reasoning through available information." if llm_resp.tool_calls else None
            )
            if not llm_resp.tool_calls:
                if llm_resp.finish_reason == "refusal":
                    return self._response(state, llm_resp.content, 0.0)  # declined: no confidence, no scraping
                _collect_from_text(llm_resp.content, state)
                response = self._response(state, llm_resp.content, 0.98)
                response.truncated = llm_resp.finish_reason in LENGTH_STOP_REASONS  # cut by its output cap (#9)
                return response

            # One assistant message per LLM turn carrying every tool call (piece E2).
            messages.append(self._assistant_turn(llm_resp))
            for tc in llm_resp.tool_calls:
                output = await self._execute_tool_call(tc, trusted, state, thought, iteration, auth)
                messages.append(self._tool_message(tc, output))

        # Step limit reached (#5): one more call with tool use forbidden, answering from the results gathered so far.
        final_resp = await self.llm_gateway.chat(
            messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id, pii=state.pii,
            text_only=True,
        )
        _collect_from_text(final_resp.content or "", state)
        reply = final_resp.content or (
            "I reached the step limit before finishing. The tool results gathered so far are attached."
        )
        response = self._response(state, reply, 0.60)
        response.partial = True
        response.truncated = final_resp.finish_reason in LENGTH_STOP_REASONS
        return response

    async def run_stream(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        *,
        auth: AuthContext,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time SSE events for thoughts, tool dispatches, final tokens and the resolved intent."""
        tenant_id, state, messages, tools, trusted = self._prepare_session(
            user_message, jurisdiction, context, history, auth
        )
        yield {"event": "thinking", "data": f"Analyzing request in jurisdiction [{jurisdiction}]..."}

        for iteration in range(1, self.max_iterations + 1):
            llm_resp = await self.llm_gateway.chat(
                messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id, pii=state.pii
            )
            thought = llm_resp.thought_process or (
                f"Step {iteration}: Reasoning through available information." if llm_resp.tool_calls else None
            )
            if llm_resp.thought_process:
                yield {"event": "thinking", "data": llm_resp.thought_process}

            if not llm_resp.tool_calls:
                for piece in re.findall(r"\S+\s*|\s+", llm_resp.content or ""):
                    yield {"event": "token", "data": piece}
                truncated = llm_resp.finish_reason in LENGTH_STOP_REASONS
                yield {
                    "event": "done",
                    "data": {
                        "finish_reason": llm_resp.finish_reason,
                        "truncated": truncated,
                    },
                }
                yield {"event": "result", "data": _result_data(state, partial=False, truncated=truncated)}
                yield {"event": "intent", "data": self._intent(state).value}
                return

            messages.append(self._assistant_turn(llm_resp))
            for tc in llm_resp.tool_calls:
                yield {"event": "tool_call", "data": {"name": tc.name, "arguments": tc.arguments}}
                output = await self._execute_tool_call(tc, trusted, state, thought, iteration, auth)
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
                messages.append(self._tool_message(tc, output))

        # After max_iterations rounds still requesting tools:
        yield {"event": "partial", "data": True}
        # Same tools as earlier turns so the cached prefix matches; the gateway forbids their use here (agy H-F1).
        truncated = False
        async for chunk in self.llm_gateway.chat_stream(messages, tools=tools, tenant_id=tenant_id, pii=state.pii):
            if chunk.get("event") == "done":
                data = dict(chunk.get("data") or {})
                truncated = data.get("finish_reason") in LENGTH_STOP_REASONS
                data["truncated"] = truncated
                yield {**chunk, "data": data}
            else:
                yield chunk
        yield {"event": "result", "data": _result_data(state, partial=True, truncated=truncated)}
        yield {"event": "intent", "data": self._intent(state).value}
