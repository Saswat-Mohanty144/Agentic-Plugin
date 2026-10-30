"""Provider clients: Anthropic via the official SDK, retries, config fail-loud (piece E: E1, E2, E3, E6, E7)."""

import json

import anthropic
import httpx
import httpx2
import pytest

from hrms_plugin.ai.llm import (
    AnthropicClient,
    ChatMessage,
    GeminiClient,
    LLMConfig,
    LLMGateway,
    LLMNotConfigured,
    LLMProvider,
    OpenAICompatibleClient,
    ToolCall,
    ToolDefinition,
)

TOOL = ToolDefinition(
    name="search_labor_regulations",
    description="d",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


def anthropic_reply(content, stop_reason="end_turn", **extra):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 3},
        **extra,
    }


class Wire:
    """Records request bodies/headers and replays scripted JSON replies over a mock transport."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, request):
        self.requests.append({"body": json.loads(request.content), "headers": dict(request.headers)})
        return httpx2.Response(200, json=self.replies.pop(0))


def anthropic_client(wire, **cfg):
    config = LLMConfig(
        provider=LLMProvider.ANTHROPIC,
        model=cfg.pop("model", "claude-opus-5-5"),
        api_key="k",
        max_tokens=16000,
        effort=cfg.pop("effort", "medium"),
    )
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wire.handler))
    return AnthropicClient(config, http_client=http)


# E1 — request shape on the wire ---------------------------------------------------------------


async def test_anthropic_request_shape():
    wire = Wire(anthropic_reply([{"type": "text", "text": "hi"}]))
    resp = await anthropic_client(wire).chat(
        [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="q")],
        tools=[TOOL],
        temperature=0.2,
    )
    body, headers = wire.requests[0]["body"], wire.requests[0]["headers"]
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000
    assert body["output_config"] == {"effort": "medium"}
    assert body["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["tools"] == [{"name": "search_labor_regulations", "description": "d", "input_schema": TOOL.parameters}]
    assert "temperature" not in body  # 400 on every model since 2026-10-21
    assert body["messages"] == [{"role": "user", "content": "q"}]
    assert resp.content == "hi" and resp.provider == "anthropic"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_every_effort_level_is_sent(effort):
    wire = Wire(anthropic_reply([{"type": "text", "text": "x"}]))
    await anthropic_client(wire, effort=effort).chat([ChatMessage(role="user", content="q")])
    assert wire.requests[0]["body"]["output_config"] == {"effort": effort}


# E2 — tool-use replay -------------------------------------------------------------------------


async def test_tool_use_round_trip_preserves_thinking_and_groups_results():
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    wire = Wire(
        anthropic_reply(
            [
                thinking,
                {"type": "tool_use", "id": "tu_1", "name": "search_labor_regulations", "input": {"query": "a"}},
                {"type": "tool_use", "id": "tu_2", "name": "search_labor_regulations", "input": {"query": "b"}},
            ],
            stop_reason="tool_use",
        ),
        anthropic_reply([{"type": "text", "text": "done"}]),
    )
    client = anthropic_client(wire)
    first = await client.chat([ChatMessage(role="user", content="q")], tools=[TOOL])
    assert [tc.id for tc in first.tool_calls] == ["tu_1", "tu_2"]
    history = [
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="assistant", content=first.content, tool_calls=first.tool_calls, raw_content=first.raw_content
        ),
        ChatMessage(role="tool", content='{"r": 1}', tool_call_id="tu_1", name="search_labor_regulations"),
        ChatMessage(role="tool", content='{"r": 2}', tool_call_id="tu_2", name="search_labor_regulations"),
    ]
    await client.chat(history, tools=[TOOL])
    sent = wire.requests[1]["body"]["messages"]
    assert len(sent) == 3
    assert sent[1]["role"] == "assistant" and sent[1]["content"][0] == thinking  # replayed byte-identical
    assert [b["id"] for b in sent[1]["content"] if b["type"] == "tool_use"] == ["tu_1", "tu_2"]
    assert sent[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"r": 1}'},
            {"type": "tool_result", "tool_use_id": "tu_2", "content": '{"r": 2}'},
        ],
    }


async def test_assistant_without_raw_content_builds_tool_use_blocks():
    wire = Wire(anthropic_reply([{"type": "text", "text": "ok"}]))
    history = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="t", arguments={"a": 1})]),
        ChatMessage(role="tool", content="{}", tool_call_id="c1", name="t"),
    ]
    await anthropic_client(wire).chat(history)
    assistant = wire.requests[0]["body"]["messages"][1]
    assert assistant == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "t", "input": {"a": 1}}],
    }


# E3 — refusal -----------------------------------------------------------------------------------


async def test_refusal_is_reported_without_tool_calls():
    wire = Wire(
        anthropic_reply(
            [], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}
        )
    )
    resp = await anthropic_client(wire).chat([ChatMessage(role="user", content="q")])
    assert resp.finish_reason == "refusal" and resp.tool_calls == []
    assert resp.content == "This request was declined by the model provider's safety system."
    assert resp.raw_content is None


# E6 — fail loud on missing / bad configuration ---------------------------------------------------------

_PROVIDER_VARS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "NVIDIA_API_KEY",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_nothing_configured_raises(clean_env):
    with pytest.raises(LLMNotConfigured):
        LLMGateway()


def test_unknown_provider_raises(clean_env):
    clean_env.setenv("LLM_PROVIDER", "bard")
    with pytest.raises(LLMNotConfigured):
        LLMGateway()


def test_explicit_fake_is_allowed(clean_env):
    clean_env.setenv("LLM_PROVIDER", "fake")
    assert LLMGateway().config.provider is LLMProvider.FAKE


def test_anthropic_defaults_from_env(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    config = LLMGateway().config
    assert (config.provider, config.model, config.max_tokens, config.effort) == (
        LLMProvider.ANTHROPIC,
        "claude-opus-5-5",
        16000,
        "medium",
    )


def test_model_and_effort_overrides(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    clean_env.setenv("LLM_MODEL", "claude-sonnet-5-5")
    clean_env.setenv("LLM_EFFORT", "high")
    config = LLMGateway().config
    assert (config.model, config.effort) == ("claude-sonnet-5-5", "high")


@pytest.mark.parametrize("effort", ["HIGH", "extreme"])
def test_invalid_effort_raises(clean_env, effort):
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    clean_env.setenv("LLM_EFFORT", effort)
    with pytest.raises(LLMNotConfigured):
        LLMGateway()


def test_empty_effort_defaults_to_medium(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "k")
    clean_env.setenv("LLM_EFFORT", "")
    assert LLMGateway().config.effort == "medium"


# E7 — retries, client reuse, Gemini key header ------------------------------------------------------------


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def openai_client(responses, sleeps):
    seen = []

    def handler(request):
        seen.append(request)
        status, headers, body = responses.pop(0)
        return httpx.Response(status, headers=headers, json=body)

    config = LLMConfig(provider=LLMProvider.OPENAI, model="m", api_key="k")
    client = OpenAICompatibleClient(
        config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleeps
    )
    return client, seen


OK = (200, {}, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


async def test_429_then_success_retries_once_with_backoff():
    sleeps = Sleeps()
    client, seen = openai_client([(429, {}, {}), OK], sleeps)
    assert (await client.chat([ChatMessage(role="user", content="q")])).content == "ok"
    assert len(seen) == 2 and sleeps.calls == [0.5]


async def test_retry_after_is_honoured_and_capped():
    sleeps = Sleeps()
    client, _ = openai_client([(503, {"retry-after": "3"}, {}), (529, {"retry-after": "99"}, {}), OK], sleeps)
    await client.chat([ChatMessage(role="user", content="q")])
    assert sleeps.calls == [3.0, 30.0]


async def test_400_is_not_retried():
    sleeps = Sleeps()
    client, seen = openai_client([(400, {}, {"error": "bad"})], sleeps)
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat([ChatMessage(role="user", content="q")])
    assert len(seen) == 1 and sleeps.calls == []


async def test_gives_up_after_two_retries():
    sleeps = Sleeps()
    client, seen = openai_client([(503, {}, {}), (503, {}, {}), (503, {}, {})], sleeps)
    with pytest.raises(httpx.HTTPStatusError):
        await client.chat([ChatMessage(role="user", content="q")])
    assert len(seen) == 3 and sleeps.calls == [0.5, 1.0]


async def test_one_http_client_is_reused():
    client, _ = openai_client([OK, OK], Sleeps())
    first = client._http
    await client.chat([ChatMessage(role="user", content="a")])
    await client.chat([ChatMessage(role="user", content="b")])
    assert client._http is first


async def test_gemini_key_goes_in_header_not_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        )

    config = LLMConfig(provider=LLMProvider.GEMINI, model="gemini-2.5-flash", api_key="secret-key")
    client = GeminiClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=Sleeps())
    await client.chat([ChatMessage(role="user", content="q")])
    assert "secret-key" not in str(seen[0].url)
    assert seen[0].headers["x-goog-api-key"] == "secret-key"
