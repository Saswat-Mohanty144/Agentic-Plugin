"""Deterministic LLM double: returns scripted responses in order and fails on surplus or leftover calls."""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Tuple

from hrms_plugin.ai.llm import (
    BaseLLMClient,
    ChatMessage,
    LLMConfig,
    LLMGateway,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)


class ScriptedLLMClient(BaseLLMClient):
    def __init__(self, responses: Sequence[LLMResponse]):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE, model="scripted"))
        self._responses: List[LLMResponse] = list(responses)
        self.calls: List[Dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._responses)

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError(f"ScriptedLLMClient received unscripted call #{len(self.calls)}")
        return self._responses.pop(0)

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        response = await self.chat(messages, tools, temperature)
        for word in response.content.split(" ") if response.content else []:
            yield {"event": "token", "data": word + " "}
        yield {"event": "done", "data": {"finish_reason": response.finish_reason}}


def tool_call(name: str, **arguments: Any) -> LLMResponse:
    return tool_calls((name, arguments))


def tool_calls(*calls: Tuple[str, Dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=n, arguments=a) for n, a in calls],
        finish_reason="tool_calls",
        model="scripted",
        provider="scripted",
    )


def final(text: str) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop", model="scripted", provider="scripted")


def scripted_gateway(responses: Iterable[LLMResponse]) -> Tuple[LLMGateway, ScriptedLLMClient]:
    gateway = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    client = ScriptedLLMClient(list(responses))
    gateway.client = client
    return gateway, client


def assert_consumed(clients: Iterable[ScriptedLLMClient]) -> None:
    leftovers = sum(c.remaining for c in clients)
    if leftovers:
        raise AssertionError(f"{leftovers} scripted response(s) left unused")
