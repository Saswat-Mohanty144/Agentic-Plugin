"""Token waste #9: output spend is bounded per task, not one 4,096 cap for everything.

OpenAI-compatible and Gemini clients get a per-task max_tokens. Anthropic's max_tokens also bounds adaptive thinking,
so it keeps its cap and light tasks run at effort "low" instead (decision logged in the spec). A reply stopped by
its cap is marked truncated.
"""

import asyncio
import json

import httpx
import pytest

from hrms_plugin.ai.llm import (
    TASK_MAX_TOKENS,
    ChatMessage,
    GeminiClient,
    LLMConfig,
    LLMGateway,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleClient,
    TaskClass,
)
from hrms_plugin.security.tenant_registry import LlmPolicy

MSG = [ChatMessage(role="user", content="hi")]
POLICY = LlmPolicy(allowed_providers=("fake", "openai", "gemini", "anthropic"), max_data_tier=2)


def openai_gateway(bodies):
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    config = LLMConfig(provider=LLMProvider.OPENAI, model="m", api_key="k", base_url="https://api.test/v1")
    gw = LLMGateway(config=config, policy_resolver=lambda t, p: POLICY)
    gw.client = OpenAICompatibleClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return gw


@pytest.mark.parametrize("task, cap", [(TaskClass.CLASSIFICATION, 512), (TaskClass.SCORING, 512),
                                       (TaskClass.EXTRACTION, 1024), (TaskClass.AGENT_REACT, 2048),
                                       (TaskClass.POLICY_QA, 2048), (TaskClass.DRAFTING, 4096)])
def test_openai_payload_carries_the_task_cap(task, cap):
    bodies = []
    asyncio.run(openai_gateway(bodies).chat(MSG, task_class=task, tenant_id="acme"))
    assert bodies[0]["max_tokens"] == cap == TASK_MAX_TOKENS[task]


def test_every_task_class_has_a_cap():
    assert set(TASK_MAX_TOKENS) == set(TaskClass)


def test_gemini_payload_carries_the_task_cap():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                                         "finishReason": "STOP"}]})

    config = LLMConfig(provider=LLMProvider.GEMINI, model="g", api_key="k")
    gw = LLMGateway(config=config, policy_resolver=lambda t, p: POLICY)
    gw.client = GeminiClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(gw.chat(MSG, task_class=TaskClass.EXTRACTION, tenant_id="acme"))
    assert bodies[0]["generationConfig"]["maxOutputTokens"] == 1024


class Recorder:
    """Client double that records the per-call options the gateway passes."""

    supports_tool_choice_none = False
    stream_reports_usage = True

    def __init__(self, accepts_max_tokens=False, accepts_effort=False):
        self.accepts_max_tokens = accepts_max_tokens
        self.accepts_effort = accepts_effort
        self.kwargs = []

    async def chat(self, messages, tools=None, temperature=None, **kw):
        self.kwargs.append(kw)
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.mark.parametrize("task, effort", [(TaskClass.CLASSIFICATION, "low"), (TaskClass.EXTRACTION, "low"),
                                          (TaskClass.AGENT_REACT, None), (TaskClass.DRAFTING, None)])
def test_anthropic_style_client_gets_effort_not_a_token_cap(task, effort):
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE), policy_resolver=lambda t, p: POLICY)
    gw.client = Recorder(accepts_effort=True)
    asyncio.run(gw.chat(MSG, task_class=task, tenant_id="acme"))
    assert gw.client.kwargs[0].get("effort") == effort and "max_tokens" not in gw.client.kwargs[0]


def test_clients_that_declare_nothing_get_no_extra_options():
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE), policy_resolver=lambda t, p: POLICY)
    gw.client = Recorder()
    asyncio.run(gw.chat(MSG, task_class=TaskClass.CLASSIFICATION, tenant_id="acme"))
    assert gw.client.kwargs == [{}]


def test_anthropic_client_sends_the_effort_override():
    from hrms_plugin.ai.llm import AnthropicClient

    assert AnthropicClient.accepts_effort is True and AnthropicClient.accepts_max_tokens is False


@pytest.mark.parametrize("reason, truncated", [("length", True), ("max_tokens", True), ("MAX_TOKENS", True),
                                               ("stop", False), ("end_turn", False)])
def test_final_answer_stopped_by_its_cap_is_marked_truncated(scripted, reason, truncated):
    from hrms_plugin.agents.orchestrator import AgenticOrchestrator
    from hrms_plugin.agents.tools import AgenticToolRegistry
    from hrms_plugin.security.rbac import AuthContext, UserRole

    gateway, _ = scripted(LLMResponse(content="partial ans", finish_reason=reason, model="s", provider="s"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = asyncio.run(orch.run(user_message="hi", auth=AuthContext(
        user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))))
    assert resp.truncated is truncated
