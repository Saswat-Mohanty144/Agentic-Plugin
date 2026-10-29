"""Token waste #8: the cacheable prefix is identical across requests, and cache hits are measured for every provider.

OpenAI's prompt_tokens and Gemini's promptTokenCount already include cached tokens, so the cached part is moved to
cache_read_input_tokens and never counted twice against a budget. Found while checking: the Gemini client reported
no usage at all, so Gemini spend never counted against the daily budget.
"""

import asyncio
import json

import httpx
import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ChatMessage, GeminiClient, LLMConfig, LLMGateway, LLMProvider, OpenAICompatibleClient
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.scripted_llm import final, scripted_gateway

POLICY = LlmPolicy(allowed_providers=("openai", "gemini"), max_data_tier=2, daily_token_budget=100000)
MSG = [ChatMessage(role="user", content="hi")]


@pytest.mark.parametrize("usage, expected", [
    ({"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800}},
     {"input_tokens": 200, "output_tokens": 50, "cache_read_input_tokens": 800}),
    ({"prompt_tokens": 1000, "completion_tokens": 50},
     {"input_tokens": 1000, "output_tokens": 50}),
    ({"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 0}},
     {"input_tokens": 1000, "output_tokens": 50, "cache_read_input_tokens": 0}),
    ({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90},
     {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90}),
])
def test_usage_mapping_never_double_counts_cached_tokens(usage, expected):
    assert LLMGateway._usage_fields(usage) == expected


def _gateway(client_cls, config, handler):
    log = EgressLog(MemoryKeyValueStore())
    gw = LLMGateway(config=config, egress_log=log, policy_resolver=lambda t, p: POLICY)
    gw.client = client_cls(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return gw, log


def test_openai_cache_hits_reach_the_egress_log_and_budget():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "model": "m",
            "usage": {"prompt_tokens": 1200, "completion_tokens": 30,
                      "prompt_tokens_details": {"cached_tokens": 1024}}})

    config = LLMConfig(provider=LLMProvider.OPENAI, model="m", api_key="k", base_url="https://api.test/v1")
    gw, log = _gateway(OpenAICompatibleClient, config, handler)
    asyncio.run(gw.chat(MSG, tenant_id="acme"))
    [row] = log.list("acme")
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_input_tokens"]) == (176, 30, 1024)
    assert log.tokens_today("acme") == 1230


def test_gemini_usage_is_reported_including_cached_tokens():
    def handler(request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 40, "cachedContentTokenCount": 600}})

    config = LLMConfig(provider=LLMProvider.GEMINI, model="g", api_key="k")
    gw, log = _gateway(GeminiClient, config, handler)
    asyncio.run(gw.chat(MSG, tenant_id="acme"))
    [row] = log.list("acme")
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_input_tokens"]) == (300, 40, 600)
    assert log.tokens_today("acme") == 940


def test_gemini_without_usage_metadata_records_nothing_rather_than_guessing():
    def handler(request):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                                         "finishReason": "STOP"}]})

    gw, log = _gateway(GeminiClient, LLMConfig(provider=LLMProvider.GEMINI, model="g", api_key="k"), handler)
    asyncio.run(gw.chat(MSG, tenant_id="acme"))
    assert log.list("acme")[0]["input_tokens"] is None


def test_cacheable_prefix_is_identical_across_tenants_and_contexts():
    def first_call(tenant, context, message):
        gateway, client = scripted_gateway([final("ok")])
        orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
        auth = AuthContext(user_id="u", tenant_id=tenant, roles=frozenset({UserRole.SUPER_ADMIN}))
        asyncio.run(orch.run(user_message=message, context=context, auth=auth))
        return client.calls[0]

    a = first_call("acme", {"entity": "E1", "salary": 5}, "calculate eosb please")
    b = first_call("globex", {"route": "/x"}, "eosb for my team")
    assert a["messages"][0].content == b["messages"][0].content
    assert json.dumps([t.name for t in a["tools"]]) == json.dumps([t.name for t in b["tools"]])
    assert "acme" not in a["messages"][0].content and "E1" not in a["messages"][0].content
