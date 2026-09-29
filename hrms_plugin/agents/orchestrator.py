"""Autonomous Agentic Orchestrator implementing Multi-Turn ReAct Reasoning.

Enables the AI Plugin to execute complex, multi-step agentic workflows:
Think -> Act (Tool Call) -> Observe -> Reflect -> Act -> Final Answer
With Human-In-The-Loop proposal drafting and streaming Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from pydantic import BaseModel

from hrms_plugin.agents.supervisor import AgentResponse, UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ChatMessage, LLMGateway, TaskClass

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


class AgenticExecutionStep(BaseModel):
    step_number: int
    thought: Optional[str] = None
    tool_call: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None


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

    async def run(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResponse:
        """Execute autonomous ReAct agent loop until final answer is formulated."""
        ctx = context or {}
        context_str = json.dumps(ctx) if ctx else "None provided"

        system_msg = (
            f"{SUPER_AGENT_SYSTEM_PROMPT}\n"
            f"Current Jurisdiction: {jurisdiction}\n"
            f"Tenant ID: {tenant_id}\n"
            f"Context: {context_str}"
        )

        messages: List[ChatMessage] = [ChatMessage(role="system", content=system_msg)]

        # Append prior conversation history if provided
        if history:
            for h in history:
                messages.append(ChatMessage(role=h.get("role", "user"), content=h.get("content", "")))

        messages.append(ChatMessage(role="user", content=user_message))

        tools = self.tool_registry.get_definitions()
        executed_steps: List[AgenticExecutionStep] = []
        collected_citations: List[str] = []
        collected_actions: List[Dict[str, Any]] = []
        last_tool_data: Optional[Dict[str, Any]] = None

        for iteration in range(1, self.max_iterations + 1):
            llm_resp = await self.llm_gateway.chat(
                messages=messages,
                tools=tools,
                task_class=TaskClass.AGENT_REACT,
                tenant_id=tenant_id,
            )

            # Record thought
            thought = llm_resp.thought_process or (
                f"Step {iteration}: Reasoning through available information." if llm_resp.tool_calls else None
            )

            # If no tool calls, the agent has reached its final answer
            if not llm_resp.tool_calls:
                final_content = llm_resp.content

                # Extract citations if found in text or previous observations
                if (
                    "decree" in final_content.lower()
                    or "article" in final_content.lower()
                    or "act" in final_content.lower()
                ):
                    for line in final_content.split("\n"):
                        if (
                            any(k in line.lower() for k in ["article", "decree-law", "act", "section"])
                            and len(line) < 120
                        ):
                            collected_citations.append(line.strip(" -•*"))

                return AgentResponse(
                    intent=UserIntent.GENERAL_HR,
                    routed_agent="AgenticBrain",
                    reply_text=final_content,
                    confidence_score=0.98,
                    structured_data=last_tool_data,
                    statutory_citations=list(dict.fromkeys(collected_citations)),
                    suggested_actions=collected_actions,
                    thought_process=[s.thought for s in executed_steps if s.thought],
                    tool_calls=[s.tool_call for s in executed_steps if s.tool_call],
                )

            # Process tool calls
            for tc in llm_resp.tool_calls:
                step = AgenticExecutionStep(
                    step_number=iteration,
                    thought=thought,
                    tool_call={"id": tc.id, "name": tc.name, "arguments": tc.arguments},
                )

                # Execute tool
                tool_output = await self.tool_registry.execute(tc.name, tc.arguments)
                last_tool_data = tool_output
                step.observation = tool_output
                executed_steps.append(step)

                # Extract statutory citations from tool results
                if isinstance(tool_output, dict):
                    if "statutory_citation" in tool_output and tool_output["statutory_citation"]:
                        collected_citations.append(str(tool_output["statutory_citation"]))
                    if "statutory_citations" in tool_output and isinstance(tool_output["statutory_citations"], list):
                        collected_citations.extend(tool_output["statutory_citations"])
                    if "citations" in tool_output and isinstance(tool_output["citations"], list):
                        for c in tool_output["citations"]:
                            if isinstance(c, dict) and "citation" in c:
                                collected_citations.append(c["citation"])
                    if "violations" in tool_output and isinstance(tool_output["violations"], list):
                        for v in tool_output["violations"]:
                            if isinstance(v, dict):
                                if "statutory_citation" in v:
                                    collected_citations.append(v["statutory_citation"])
                                if "remediation_patch" in v and v["remediation_patch"]:
                                    collected_actions.append(
                                        {
                                            "action": "REMEDIATE_PATCH",
                                            "violation_id": v.get("violation_id", "VIOLATION-01"),
                                            "patch": v["remediation_patch"],
                                        }
                                    )

                # Append assistant message with tool calls & tool response to memory
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=llm_resp.content,
                        tool_calls=[tc],
                    )
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=json.dumps(tool_output),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )

        # Max iterations reached, return latest synthesis
        return AgentResponse(
            intent=UserIntent.GENERAL_HR,
            routed_agent="AgenticBrain",
            reply_text=f"Completed {len(executed_steps)} agentic reasoning steps. Review findings above.",
            confidence_score=0.90,
            structured_data=last_tool_data,
            statutory_citations=list(dict.fromkeys(collected_citations)),
            suggested_actions=collected_actions,
            thought_process=[s.thought for s in executed_steps if s.thought],
            tool_calls=[s.tool_call for s in executed_steps if s.tool_call],
        )

    async def run_stream(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time SSE events for thoughts, tool dispatches, and final tokens."""
        ctx = context or {}
        tools = self.tool_registry.get_definitions()
        yield {"event": "thinking", "data": f"Analyzing request in jurisdiction [{jurisdiction}]..."}

        system_msg = (
            f"{SUPER_AGENT_SYSTEM_PROMPT}\n"
            f"Current Jurisdiction: {jurisdiction}\n"
            f"Tenant ID: {tenant_id}\n"
            f"Context: {json.dumps(ctx)}"
        )
        messages: List[ChatMessage] = [
            ChatMessage(role="system", content=system_msg),
            ChatMessage(role="user", content=user_message),
        ]

        llm_resp = await self.llm_gateway.chat(
            messages=messages,
            tools=tools,
            task_class=TaskClass.AGENT_REACT,
            tenant_id=tenant_id,
        )

        if llm_resp.thought_process:
            yield {"event": "thinking", "data": llm_resp.thought_process}

        if llm_resp.tool_calls:
            for tc in llm_resp.tool_calls:
                yield {"event": "tool_call", "data": {"name": tc.name, "arguments": tc.arguments}}
                tool_output = await self.tool_registry.execute(tc.name, tc.arguments)
                yield {"event": "tool_observation", "data": {"name": tc.name, "output": tool_output}}

                # Add to history for final turn
                messages.append(ChatMessage(role="assistant", content="", tool_calls=[tc]))
                messages.append(
                    ChatMessage(role="tool", content=json.dumps(tool_output), tool_call_id=tc.id, name=tc.name)
                )

            # Stream final synthesized response
            yield {"event": "thinking", "data": "Synthesizing verified compliant response..."}
            async for chunk in self.llm_gateway.chat_stream(messages, tenant_id=tenant_id):
                yield chunk
        else:
            # Direct token streaming
            async for chunk in self.llm_gateway.chat_stream(messages, tenant_id=tenant_id):
                yield chunk
