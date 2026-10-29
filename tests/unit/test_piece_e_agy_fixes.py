"""Fixes for agy piece-E findings F1(part)-F11."""

import json
import math

import httpx
import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import (
    REFUSAL_TEXT,
    AnthropicClient,
    ChatMessage,
    LLMConfig,
    LLMNotConfigured,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleClient,
    ToolCall,
    _retry_delay,
)
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final

ADMIN = AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


# F1 (valid part) / F2 / F3 — message sequence rules -----------------------------------------------

def test_conversation_starting_with_assistant_is_rejected():
    with pytest.raises(ValueError, match="first message must be from the user"):
        AnthropicClient._convert([ChatMessage(role="assistant", content="a"), ChatMessage(role="user", content="u")])


def test_consecutive_user_turns_are_allowed():  # the API merges them (claude-api reference) — F1 disputed part
    _system, msgs = AnthropicClient._convert([ChatMessage(role="user", content="u1"),
                                              ChatMessage(role="user", content="u2")])
    assert [m["role"] for m in msgs] == ["user", "user"]


def test_empty_user_content_is_not_sent():
    _system, msgs = AnthropicClient._convert([ChatMessage(role="user", content=""),
                                              ChatMessage(role="user", content="u")])
    assert msgs == [{"role": "user", "content": "u"}]


def test_orphan_tool_result_is_rejected():
    with pytest.raises(ValueError, match="tool result without a matching tool call"):
        AnthropicClient._convert([ChatMessage(role="user", content="q"),
                                  ChatMessage(role="tool", content="{}", tool_call_id="call_x")])


def test_tool_result_for_another_call_id_is_rejected():
    with pytest.raises(ValueError, match="tool result without a matching tool call"):
        AnthropicClient._convert([
            ChatMessage(role="user", content="q"),
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="t", arguments={})]),
            ChatMessage(role="tool", content="{}", tool_call_id="c2"),
        ])


@pytest.mark.parametrize("history,status", [
    ([{"role": "user", "content": ""}], 422),
    ([{"role": "assistant", "content": "hi"}], 422),
    ([{"role": "assistant", "content": "hi"}, {"role": "user", "content": "x"}], 422),
    ([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}], 200),
])
def test_history_must_start_with_user_and_be_non_empty(make_client, bearer, history, status):
    body = {"message": "hi", "jurisdiction": "IN", "history": history}
    res = make_client().post("/v1/chat", headers=bearer(), json=body)
    assert res.status_code == status


# F4 — Retry-After variants -------------------------------------------------------------------------

def test_retry_after_http_date_is_honoured():
    future = "Wed, 21 Oct 2099 07:28:00 GMT"
    assert _retry_delay(httpx.Response(429, headers={"retry-after": future}), 0) == 30.0  # far future → capped


def test_retry_after_past_http_date_means_no_wait():
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}), 0) == 0.0


@pytest.mark.parametrize("value", ["nan", "inf", "-5", "soon"])
def test_unusable_retry_after_falls_back_to_backoff(value):
    delay = _retry_delay(httpx.Response(429, headers={"retry-after": value}), 1)
    assert not math.isnan(delay) and delay == 1.0


# F5 / F9 — client lifecycle -----------------------------------------------------------------------------

async def test_default_http_client_is_created_once_and_closed(monkeypatch):
    created = []
    real = httpx.AsyncClient

    def counting(*args, **kwargs):
        client = real(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", counting)
    client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENAI, model="m", api_key="k"))
    assert len(created) == 1 and client._http is created[0]
    await client.aclose()
    assert created[0].is_closed


async def test_injected_http_client_is_not_closed_by_us():
    injected = httpx.AsyncClient()
    config = LLMConfig(provider=LLMProvider.OPENAI, model="m", api_key="k")
    client = OpenAICompatibleClient(config, http_client=injected)
    await client.aclose()
    assert not injected.is_closed
    await injected.aclose()


async def test_synthesizer_closes_the_gateway_it_created(monkeypatch):
    from hrms_plugin.ai import llm
    from hrms_plugin.schema.canonical import EntityType
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
    from hrms_plugin.schema.synthesizer import MappingSynthesizer

    closed = []
    monkeypatch.setattr(llm.LLMGateway, "aclose", lambda self: closed.append(self) or _done())
    discovered = DiscoveredEntity(name="E", fields={"zq_given": DiscoveredField(name="zq_given", path="zq_given")})
    # tenant with an LLM policy (piece G2: the DEFAULT tenant has none and is refused)
    await MappingSynthesizer.synthesize_agentic(discovered, EntityType.EMPLOYEE, tenant_id="acme")
    assert len(closed) == 1


async def _done():
    return None


# F6 / F7 — streaming history and 503 -------------------------------------------------------------------

async def test_stream_includes_history(scripted):
    gateway, client = scripted(final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    [e async for e in orch.run_stream("now", jurisdiction="AE", auth=ADMIN,
                                      history=[{"role": "user", "content": "before"},
                                               {"role": "assistant", "content": "reply"}])]
    assert [m.content for m in client.calls[0]["messages"] if m.role != "system"] == ["before", "reply", "now"]


def test_stream_returns_503_when_llm_not_configured(make_client, bearer, monkeypatch):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    monkeypatch.setenv("LLM_PROVIDER", "bard")
    res = make_client(supervisor_agent=SupervisorAgent()).post(
        "/v1/chat/stream", headers=bearer(), json={"message": "hi", "jurisdiction": "IN"})
    assert res.status_code == 503 and res.json() == {"error": "LLM_NOT_CONFIGURED"}


def test_stream_passes_history_through_the_api(make_client, bearer, scripted):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    gateway, client = scripted(final("ok"))
    res = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway)).post(
        "/v1/chat/stream", headers=bearer(),
        json={"message": "now", "jurisdiction": "IN", "history": [{"role": "user", "content": "before"}]})
    assert res.status_code == 200
    assert "before" in [m.content for m in client.calls[0]["messages"]]


# F8 — refusal has its own handling -------------------------------------------------------------------------

async def test_refusal_reply_has_zero_confidence_and_no_scraped_citations(scripted):
    gateway, _ = scripted(LLMResponse(content=REFUSAL_TEXT + "\nSection 9 of the Act", finish_reason="refusal"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = await orch.run("q", jurisdiction="AE", auth=ADMIN)
    assert resp.confidence_score == 0.0 and resp.statutory_citations == []


async def test_normal_answer_keeps_high_confidence(scripted):
    gateway, _ = scripted(final("plain answer"))
    resp = await AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry()).run(
        "q", jurisdiction="AE", auth=ADMIN)
    assert resp.confidence_score == 0.98


# F10 — effort validated at client construction ------------------------------------------------------------------

def test_invalid_effort_in_config_is_rejected():
    with pytest.raises(LLMNotConfigured):
        AnthropicClient(LLMConfig(provider=LLMProvider.ANTHROPIC, api_key="k", effort="bogus"))


# F11 — single-call stream is chunked -----------------------------------------------------------------------------

async def test_single_call_stream_emits_multiple_token_events(scripted):
    gateway, _ = scripted(final("one two  three\nfour"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    tokens = [e["data"] async for e in orch.run_stream("q", jurisdiction="AE", auth=ADMIN) if e["event"] == "token"]
    assert len(tokens) == 4 and "".join(tokens) == "one two  three\nfour"
    assert json.dumps(tokens)
