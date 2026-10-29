"""Strict adversarial QA tests closing coverage gaps across I/O and core modules (AREA: GAPS-IO).

Target gaps:
- hrms_plugin/agents/voice.py (Sarvam STT/TTS HTTP paths, boundaries, upstream errors, HTTP endpoints)
- hrms_plugin/ai/llm.py (Adapters, SSE streams, retry delays, gemini schema, anthropic converter, gateway egress)
- hrms_plugin/integrations/frappe.py (aclose, non-dict responses, missing name in slip listing)
- hrms_plugin/writeback/service.py (_same decimals, _valid bounds, host unconfigured, missing modified, rollback)
- hrms_plugin/payroll/fingerprint.py, rules.py, pf.py (fingerprint json types, ceiling bounds, missing earnings/abbr)
- hrms_plugin/storage/sqlite_store.py, memory_store.py, mapping_store.py (TTL purge, lazy expiry, json corruption)
"""

from __future__ import annotations

import base64
import email.utils
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import yaml
from pydantic import ValidationError

from hrms_plugin.agents.voice import VoiceAgent, VoiceUpstreamError
from hrms_plugin.ai.llm import (
    AnthropicClient,
    BaseLLMClient,
    ChatMessage,
    FakeLLMClient,
    GeminiClient,
    LLMConfig,
    LLMGateway,
    LLMNotConfigured,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleClient,
    ToolCall,
    ToolDefinition,
    _retry_delay,
)
from hrms_plugin.governance.llm_policy import LlmBudgetExceeded, LlmNotAllowed, LlmPolicy
from hrms_plugin.integrations.frappe import FrappeClient, FrappeError
from hrms_plugin.integrations.host_registry import HostConfig
from hrms_plugin.payroll.fingerprint import _json_type, slip_fingerprint
from hrms_plugin.payroll.pf import PfError, compute_pf
from hrms_plugin.payroll.rules import (
    PayrollRulesRegistry,
    PfRules,
    RulesError,
    TenantPayrollRules,
    load_payroll_rules,
)
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.mapping import EntityMapping
from hrms_plugin.security.pii import PiiEgressBlocked
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.writeback.service import WritebackError, WritebackService, _same, _valid

# ====================================================================================================
# BEHAVIOUR 1: Voice AI multimodal integration (Sarvam STT / TTS HTTP paths, mock fallback, boundaries)
# ====================================================================================================

@pytest.mark.asyncio
async def test_voice_agent_stt_sarvam_http_success(monkeypatch: pytest.MonkeyPatch):
    """Positive path: VoiceAgent transcribe_audio via Sarvam HTTP endpoint when api_key is configured."""
    recorded_requests: List[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        assert request.url.path == "/speech-to-text"
        assert request.headers.get("api-subscription-key") == "secret-sarvam-key"
        return httpx.Response(
            200,
            json={
                "transcript": "Apply casual leave for next Monday",
                "language_code": "en-IN",
                "confidence": 0.995,
            },
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="secret-sarvam-key", default_language="en-IN")
    result = await agent.transcribe_audio(b"RIFF_FAKE_AUDIO_BYTES", language_code="en-IN", model="saaras:v2")

    assert result["mocked"] is False
    assert result["transcript"] == "Apply casual leave for next Monday"
    assert result["language_code"] == "en-IN"
    assert result["confidence"] == 0.995
    assert result["model"] == "saaras:v2"
    assert len(recorded_requests) == 1


@pytest.mark.asyncio
async def test_voice_agent_tts_sarvam_http_success(monkeypatch: pytest.MonkeyPatch):
    """Positive path: VoiceAgent synthesize_speech via Sarvam HTTP endpoint when api_key is configured."""
    encoded_audio = base64.b64encode(b"WAVE_OUTPUT_BYTES").decode("utf-8")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/text-to-speech"
        assert request.headers.get("api-subscription-key") == "secret-sarvam-key"
        payload = json.loads(request.content)
        assert payload["inputs"] == ["Your leave request is approved."]
        assert payload["target_language_code"] == "hi-IN"
        assert payload["speaker"] == "meera"
        return httpx.Response(200, json={"audios": [encoded_audio]})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="secret-sarvam-key", default_language="hi-IN", target_speaker="meera")
    result = await agent.synthesize_speech("Your leave request is approved.", target_language="hi-IN")

    assert result["mocked"] is False
    assert result["audio_base64"] == encoded_audio
    assert result["audio_format"] == "audio/wav"
    assert result["speaker"] == "meera"
    assert result["language_code"] == "hi-IN"


@pytest.mark.asyncio
async def test_voice_agent_handle_voice_turn_live_pipeline(monkeypatch: pytest.MonkeyPatch):
    """Positive path: Full voice turn with live Sarvam HTTP STT and TTS mocks."""
    tts_wav = base64.b64encode(b"TTS_REPLY_WAV").decode("utf-8")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "speech-to-text" in request.url.path:
            return httpx.Response(200, json={"transcript": "Check my leave balance", "language_code": "en-IN"})
        elif "text-to-speech" in request.url.path:
            return httpx.Response(200, json={"audios": [tts_wav]})
        return httpx.Response(404)

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="sarvam-test-key")

    class FakeSupervisor:
        async def process_turn(self, user_message: str, user_id: str) -> Dict[str, Any]:
            assert user_id == "usr-42"
            assert user_message == "Check my leave balance"
            return {
                "reply": "You have 14 days of casual leave remaining.",
                "routed_agent": "LeaveAttendanceAgent",
                "actions": [],
            }

    turn_result = await agent.handle_voice_turn(
        audio_bytes=b"raw-audio",
        supervisor_agent=FakeSupervisor(),
        user_id="usr-42",
    )
    assert turn_result["transcript_in"] == "Check my leave balance"
    assert turn_result["reply_text"] == "You have 14 days of casual leave remaining."
    assert turn_result["agent_intent"] == "LeaveAttendanceAgent"
    assert turn_result["reply_audio_base64"] == tts_wav


def test_http_voice_transcribe_and_synthesize_endpoints(make_client, bearer, monkeypatch: pytest.MonkeyPatch):
    """Positive path: HTTP /v1/voice/transcribe and /v1/voice/synthesize endpoints."""
    raw_wav = b"RIFF_HEADER_12345"
    wav_b64 = base64.b64encode(raw_wav).decode("utf-8")
    reply_wav_b64 = base64.b64encode(b"REPLY_WAV").decode("utf-8")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "speech-to-text" in request.url.path:
            return httpx.Response(200, json={"transcript": "Punch check-in", "language_code": "en-IN"})
        elif "text-to-speech" in request.url.path:
            return httpx.Response(200, json={"audios": [reply_wav_b64]})
        return httpx.Response(404)

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )
    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent(api_key="sarvam-key"))

    client = make_client()
    headers = bearer(roles=["staff"])

    # 1. Transcribe
    res_stt = client.post("/v1/voice/transcribe", json={"audio_base64": wav_b64}, headers=headers)
    assert res_stt.status_code == 200
    assert res_stt.json()["transcript"] == "Punch check-in"
    assert res_stt.json()["mocked"] is False

    # 2. Synthesize
    res_tts = client.post(
        "/v1/voice/synthesize",
        json={"text": "Check-in recorded at 09:00 AM", "target_language": "en-IN"},
        headers=headers,
    )
    assert res_tts.status_code == 200
    assert res_tts.json()["audio_base64"] == reply_wav_b64
    assert res_tts.json()["mocked"] is False


@pytest.mark.asyncio
async def test_voice_agent_stt_boundary_empty_audio_and_minimal_response(monkeypatch: pytest.MonkeyPatch):
    """Boundary values: STT with empty audio bytes, and minimal response payload missing optional fields."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        # Returns only the bare minimum transcript field
        return httpx.Response(200, json={"transcript": "boundary transcript"})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="key", default_language="ta-IN")
    res = await agent.transcribe_audio(b"", language_code=None)

    assert res["transcript"] == "boundary transcript"
    assert res["language_code"] == "ta-IN"  # defaults to agent.default_language
    assert res["confidence"] == 1.0  # defaults to 1.0


@pytest.mark.asyncio
async def test_voice_agent_tts_boundary_empty_audios_array(monkeypatch: pytest.MonkeyPatch):
    """Boundary value: Sarvam TTS returns empty audios array -> audio_base64 defaults to empty string."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"audios": []})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="key")
    res = await agent.synthesize_speech("Sample text")
    assert res["audio_base64"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,lang",
    [
        ("नमस्ते मुझे कल के लिए बीमारी की छुट्टी चाहिए", "hi-IN"),  # Hindi
        ("எனக்கு நாளை விடுப்பு வேண்டும்", "ta-IN"),  # Tamil
        ("నాకు రేపు సెలవు కావాలి", "te-IN"),  # Telugu
        ("A" * 1000, "en-IN"),  # Long text
    ],
)
async def test_voice_agent_tts_unicode_and_max_size_boundary(monkeypatch: pytest.MonkeyPatch, text: str, lang: str):
    """Boundary values: Indic unicode scripts and long text boundaries for speech synthesis."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["inputs"] == [text]
        assert payload["target_language_code"] == lang
        return httpx.Response(200, json={"audios": ["b64audio_placeholder"]})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="key")
    res = await agent.synthesize_speech(text, target_language=lang)
    assert res["audio_base64"] == "b64audio_placeholder"
    assert res["language_code"] == lang


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_voice_agent_stt_and_tts_upstream_http_status_errors_negative(
    monkeypatch: pytest.MonkeyPatch, status_code: int
):
    """Negative paths: Sarvam STT and TTS raise httpx.HTTPStatusError on upstream HTTP error statuses."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": f"Upstream failure {status_code}"})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="sarvam-key")
    with pytest.raises(VoiceUpstreamError) as exc_stt:  # GAPS-IO-1: mapped to 502 by the API
        await agent.transcribe_audio(b"audio-bytes")
    assert exc_stt.value.code == "VOICE_UPSTREAM_ERROR"

    with pytest.raises(VoiceUpstreamError) as exc_tts:
        await agent.synthesize_speech("Hello")
    assert exc_tts.value.code == "VOICE_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_voice_agent_stt_and_tts_upstream_timeout_negative(monkeypatch: pytest.MonkeyPatch):
    """Negative paths: Sarvam STT and TTS timeouts surface as VOICE_UPSTREAM_TIMEOUT."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Upstream socket timed out", request=request)

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="sarvam-key")
    for call in (agent.transcribe_audio(b"audio-bytes"), agent.synthesize_speech("Hello")):
        with pytest.raises(VoiceUpstreamError) as exc:  # GAPS-IO-1: mapped to 504 by the API
            await call
        assert exc.value.code == "VOICE_UPSTREAM_TIMEOUT"


@pytest.mark.asyncio
async def test_voice_agent_stt_upstream_malformed_json_negative(monkeypatch: pytest.MonkeyPatch):
    """Negative path: Sarvam returns HTTP 200 with non-JSON/HTML proxy error body."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>502 Bad Gateway from Cloudflare</html>",
            headers={"content-type": "text/html"},
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "hrms_plugin.agents.voice.httpx.AsyncClient",
        lambda *args, **kwargs: orig_client(*args, **{**kwargs, "transport": httpx.MockTransport(mock_handler)}),
    )

    agent = VoiceAgent(api_key="sarvam-key")
    with pytest.raises(Exception):  # json.JSONDecodeError
        await agent.transcribe_audio(b"audio-bytes")


def test_http_voice_auth_matrix_and_malformed_payload_negative(make_client, bearer):
    """Negative paths: HTTP voice endpoints reject missing token, insufficient roles, and malformed base64."""
    client = make_client()

    # 1. Missing Authorization header -> 401
    res_no_auth = client.post("/v1/voice/transcribe", json={"audio_base64": "AAAA"})
    assert res_no_auth.status_code == 401

    # 2. Token without USE_COPILOT permission (e.g. guest role) -> 403
    # Use role 'auditor' which lacks USE_COPILOT
    res_wrong_role = client.post(
        "/v1/voice/transcribe",
        json={"audio_base64": "AAAA"},
        headers=bearer(roles=["auditor"]),
    )
    assert res_wrong_role.status_code == 403

    # 3. Malformed base64 audio payload -> 422 INVALID_AUDIO_BASE64
    res_bad_b64 = client.post(
        "/v1/voice/transcribe",
        json={"audio_base64": "not-valid-base64==="},
        headers=bearer(roles=["staff"]),
    )
    assert res_bad_b64.status_code == 422
    assert res_bad_b64.json()["error"] == "INVALID_AUDIO_BASE64"


# ====================================================================================================
# BEHAVIOUR 2: LLM Provider Client adapters, streaming SSE framing, retry delays, and gateway preflight
# ====================================================================================================

def test_llm_tool_definition_to_gemini_schema_positive():
    """Positive path: ToolDefinition.to_gemini_schema generates valid Gemini function declaration."""
    tool = ToolDefinition(
        name="lookup_policy",
        description="Lookup HR policy",
        parameters={"type": "object", "properties": {"section": {"type": "string"}}},
    )
    schema = tool.to_gemini_schema()
    assert schema["name"] == "lookup_policy"
    assert schema["description"] == "Lookup HR policy"
    assert "parameters" in schema
    assert schema["parameters"]["properties"]["section"]["type"] == "string"


def test_llm_chat_message_to_dict_full_fields_positive():
    """Positive path: ChatMessage.to_dict serialization with tool_calls, tool_call_id, and name."""
    tc = ToolCall(id="tc-101", name="calculate_pf", arguments={"wage": "15000"})
    msg = ChatMessage(
        role="assistant",
        content="",
        tool_calls=[tc],
        tool_call_id="tcid-202",
        name="calculate_pf",
    )
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert d["tool_call_id"] == "tcid-202"
    assert d["name"] == "calculate_pf"
    assert len(d["tool_calls"]) == 1
    assert d["tool_calls"][0]["id"] == "tc-101"
    assert d["tool_calls"][0]["type"] == "function"
    assert d["tool_calls"][0]["function"]["name"] == "calculate_pf"
    args = json.loads(d["tool_calls"][0]["function"]["arguments"])
    assert args["wage"] == "15000"


@pytest.mark.asyncio
async def test_llm_base_client_abstract_methods_positive():
    """Positive path: BaseLLMClient abstract method execution coverage."""
    client = FakeLLMClient(LLMConfig(provider=LLMProvider.FAKE))
    # Directly invoke abstract methods on base class to cover line 227 & 236
    res = await BaseLLMClient.chat(client, [])
    assert res is None
    res_stream = await BaseLLMClient.chat_stream(client, [])
    assert res_stream is None


@pytest.mark.asyncio
async def test_llm_fake_client_chat_stream_thinking_and_tools_positive():
    """Positive path: FakeLLMClient.chat_stream yields thinking events and tool_call events."""
    class ThinkingToolFakeClient(FakeLLMClient):
        async def chat(self, messages, tools=None, temperature=None):
            return LLMResponse(
                content="Final response content",
                thought_process="Analysing leave balance and statutory rules...",
                tool_calls=[ToolCall(id="tc-1", name="get_balance", arguments={"emp": "E1"})],
                finish_reason="stop",
            )

    client = ThinkingToolFakeClient(LLMConfig(provider=LLMProvider.FAKE))
    events = [c async for c in client.chat_stream([ChatMessage(role="user", content="hi")])]

    event_types = [e["event"] for e in events]
    assert "thinking" in event_types
    assert "tool_call" in event_types
    assert "done" in event_types
    thinking_event = next(e for e in events if e["event"] == "thinking")
    assert "Analysing leave balance" in thinking_event["data"]


def test_llm_openai_compatible_providers_base_urls_positive():
    """Positive path: OpenAICompatibleClient defaults base_url for GROQ, OPENROUTER, and NVIDIA."""
    groq_client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.GROQ))
    assert groq_client.base_url == "https://api.groq.com/openai/v1"

    openrouter_client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENROUTER))
    assert openrouter_client.base_url == "https://openrouter.ai/api/v1"

    nvidia_client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.NVIDIA))
    assert nvidia_client.base_url == "https://integrate.api.nvidia.com/v1"


@pytest.mark.asyncio
async def test_llm_openai_compatible_aclose_owned_http_positive():
    """Positive path: OpenAICompatibleClient aclose cleanly closes owned http client."""
    client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENAI))
    assert client._owns_http is True
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_openai_compatible_chat_with_tools_positive():
    """Positive path: OpenAICompatibleClient.chat includes tools schema and tool_choice auto."""
    recorded_payload: Dict[str, Any] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal recorded_payload
        recorded_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "get_rules", "arguments": "{\"jurisdiction\": \"IN\"}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENAI, api_key="test-key"), http_client=http_client)

    tool = ToolDefinition(name="get_rules", description="Get rules", parameters={"type": "object"})
    resp = await client.chat([ChatMessage(role="user", content="check")], tools=[tool])

    assert recorded_payload["tool_choice"] == "auto"
    assert len(recorded_payload["tools"]) == 1
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_123"
    assert resp.tool_calls[0].name == "get_rules"
    assert resp.tool_calls[0].arguments == {"jurisdiction": "IN"}


@pytest.mark.asyncio
async def test_llm_openai_compatible_chat_stream_sse_positive():
    """Positive path: OpenAICompatibleClient.chat_stream processes SSE tokens, tool deltas, and [DONE]."""
    sse_lines = [
        "data: {\"choices\": [{\"delta\": {\"content\": \"Hello \"}}]}\n\n",
        "data: {\"choices\": [{\"delta\": {\"tool_calls\": [{\"index\": 0, \"id\": \"call_1\"}]}}]}\n\n",
        ": keep-alive comment\n\n",
        "data: {bad json\n\n",
        "data: [DONE]\n\n",
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="".join(sse_lines).encode("utf-8"))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENAI, api_key="k"), http_client=http_client)

    events = [c async for c in client.chat_stream([ChatMessage(role="user", content="stream test")])]
    types = [e["event"] for e in events]
    assert "token" in types
    assert "tool_call_delta" in types
    assert "done" in types


def test_llm_anthropic_convert_messages_variants_positive():
    """Positive path: AnthropicClient._convert handles system parts, assistant content/tool_calls, raw_content."""
    raw_blocks = [{"type": "tool_use", "id": "t-1", "name": "f", "input": {}}]
    msgs = [
        ChatMessage(role="system", content="System instruction"),
        ChatMessage(role="user", content="User prompt"),
        ChatMessage(
            role="assistant",
            content="Assistant text",
            tool_calls=[ToolCall(id="t-1", name="f", arguments={})],
        ),
        ChatMessage(role="tool", content="{\"status\": \"ok\"}", tool_call_id="t-1"),
        ChatMessage(role="assistant", content="", raw_content=raw_blocks),
    ]

    system_parts, out = AnthropicClient._convert(msgs)
    assert system_parts == ["System instruction"]
    assert len(out) == 4
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "user"  # tool result placed in user turn
    assert out[3]["role"] == "assistant"


@pytest.mark.asyncio
async def test_llm_anthropic_chat_tool_choice_none_and_cache_positive():
    """Positive path: AnthropicClient.chat with tool_choice='none' and cache tokens reporting in usage."""
    class MockBetaMessages:
        async def create(self, **kwargs):
            assert kwargs.get("tool_choice") == {"type": "none"}
            assert "tools" in kwargs

            class Usage:
                input_tokens = 10
                output_tokens = 20
                cache_creation_input_tokens = 5
                cache_read_input_tokens = 2

            class ContentBlock:
                type = "text"
                text = "No tools used"

            class Response:
                stop_reason = "end_turn"
                model = "claude-opus-5-5"
                usage = Usage()
                content = [ContentBlock()]

            return Response()

    client = AnthropicClient(LLMConfig(provider=LLMProvider.ANTHROPIC, api_key="test-key"))
    client._client.beta.messages = MockBetaMessages()

    tool = ToolDefinition(name="calc", description="calc", parameters={"type": "object"})
    resp = await client.chat(
        [ChatMessage(role="user", content="calc")],
        tools=[tool],
        tool_choice="none",
    )
    assert resp.content == "No tools used"
    assert resp.usage["cache_creation_input_tokens"] == 5
    assert resp.usage["cache_read_input_tokens"] == 2


@pytest.mark.asyncio
async def test_llm_anthropic_chat_stream_events_positive():
    """Positive path: AnthropicClient.chat_stream yields usage, token, and done events."""
    client = AnthropicClient(LLMConfig(provider=LLMProvider.ANTHROPIC, api_key="k"))

    async def fake_chat(messages, tools=None, temperature=None, tool_choice=None):
        return LLMResponse(
            content="Streamed response",
            usage={"input_tokens": 15, "output_tokens": 5},
            finish_reason="end_turn",
            provider="anthropic",
        )

    client.chat = fake_chat
    events = [c async for c in client.chat_stream([ChatMessage(role="user", content="q")])]
    assert events[0]["event"] == "usage"
    assert events[1]["event"] == "token"
    assert events[2]["event"] == "done"


@pytest.mark.asyncio
async def test_llm_anthropic_aclose_positive():
    """Positive path: AnthropicClient.aclose cleanly closes underlying client."""
    client = AnthropicClient(LLMConfig(provider=LLMProvider.ANTHROPIC, api_key="k"))
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_gemini_chat_instruction_tools_function_call_positive():
    """Positive path: GeminiClient.chat formats system_instruction, tools, and parses functionCall candidates."""
    recorded_payload: Dict[str, Any] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal recorded_payload
        recorded_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "I will call the statutory PF tool.\n"},
                                {
                                    "functionCall": {
                                        "name": "calculate_pf",
                                        "args": {"employee_id": "EMP-01"},
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    client = GeminiClient(LLMConfig(provider=LLMProvider.GEMINI, api_key="gemini-key"), http_client=http_client)

    msgs = [
        ChatMessage(role="system", content="HR System Instructions"),
        ChatMessage(role="user", content="Compute PF"),
        ChatMessage(role="assistant", content="calling", tool_calls=[ToolCall(id="tc1", name="prev", arguments={})]),
        ChatMessage(role="tool", content="{\"status\": \"prev_done\"}", name="prev"),
    ]
    tool = ToolDefinition(name="calculate_pf", description="Calculate PF", parameters={"type": "object"})

    resp = await client.chat(msgs, tools=[tool])

    assert "system_instruction" in recorded_payload
    assert recorded_payload["system_instruction"]["parts"][0]["text"] == "HR System Instructions"
    assert "tools" in recorded_payload
    assert resp.content == "I will call the statutory PF tool.\n"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "calculate_pf"
    assert resp.tool_calls[0].arguments == {"employee_id": "EMP-01"}


@pytest.mark.asyncio
async def test_llm_gemini_chat_stream_and_aclose_positive():
    """Positive path: GeminiClient.chat_stream yields token and done; aclose closes owned http."""
    client = GeminiClient(LLMConfig(provider=LLMProvider.GEMINI, api_key="k"))
    assert client._owns_http is True

    async def fake_chat(messages, tools=None, temperature=None):
        return LLMResponse(content="gemini stream text", finish_reason="STOP", provider="gemini")

    client.chat = fake_chat
    events = [c async for c in client.chat_stream([ChatMessage(role="user", content="q")])]
    assert events[0]["event"] == "token"
    assert events[0]["data"] == "gemini stream text"
    assert events[1]["event"] == "done"
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_gateway_complete_helper_positive():
    """Positive path: LLMGateway.complete helper method with prompt and system_prompt."""
    gateway = LLMGateway(LLMConfig(provider=LLMProvider.FAKE))
    reply = await gateway.complete(
        prompt="What is the notice period?",
        system_prompt="You are an enterprise HR assistant.",
    )
    assert len(reply) > 0


@pytest.mark.asyncio
async def test_llm_gateway_stream_budget_fallback_chat_positive():
    """Positive path: LLMGateway.chat_stream falls back to chat and re-emits tokens when budget configured."""
    policy = LlmPolicy(allowed_providers=("fake",), max_data_tier=2, daily_token_budget=5000)

    class DummyEgressLog:
        def tokens_today(self, tenant_id: str) -> int:
            return 100

        def record(self, **kwargs) -> str:
            return "egress-1"

        def update(self, key: str, **kwargs) -> None:
            pass

    gateway = LLMGateway(
        LLMConfig(provider=LLMProvider.FAKE),
        egress_log=DummyEgressLog(),
        policy_resolver=lambda tid, prov: policy,
    )
    # Fake client has stream_reports_usage = False, so it exercises lines 1014-1018
    events = [c async for c in gateway.chat_stream([ChatMessage(role="user", content="hello")], tenant_id="acme")]
    assert any(e["event"] == "token" for e in events)
    assert any(e["event"] == "done" for e in events)


@pytest.mark.asyncio
async def test_llm_gateway_stream_usage_event_and_restorer_flush_positive():
    """Positive path: LLMGateway.chat_stream processes stream usage events and flushes restorer text."""
    class StreamReportingClient(BaseLLMClient):
        stream_reports_usage = True
        supports_tool_choice_none = True

        async def chat(self, messages, tools=None, temperature=None):
            return LLMResponse(content="ok")

        async def chat_stream(self, messages, tools=None, **kwargs):
            yield {
                "event": "usage",
                "data": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 2},
            }
            yield {"event": "token", "data": "Streamed text "}
            yield {
                "event": "done",
                "data": {"finish_reason": "stop", "usage": {"input_tokens": 10, "output_tokens": 5}},
            }

    client = StreamReportingClient(LLMConfig(provider=LLMProvider.FAKE))
    gateway = LLMGateway(LLMConfig(provider=LLMProvider.FAKE))
    gateway.client = client

    events = [c async for c in gateway.chat_stream([ChatMessage(role="user", content="stream")])]
    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "Streamed text " in tokens


def test_llm_gateway_resolve_config_from_env_all_providers_positive(monkeypatch: pytest.MonkeyPatch):
    """Positive path: LLMGateway._resolve_config_from_env parses all provider env configurations."""
    # 1. Fake
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert LLMGateway().config.provider == LLMProvider.FAKE

    # 2. OpenAI
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    assert LLMGateway().config.provider == LLMProvider.OPENAI

    # 3. Groq
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    assert LLMGateway().config.provider == LLMProvider.GROQ

    # 4. OpenRouter
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    assert LLMGateway().config.provider == LLMProvider.OPENROUTER

    # 5. Anthropic
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LLM_EFFORT", "high")
    assert LLMGateway().config.provider == LLMProvider.ANTHROPIC

    # 6. Gemini
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    assert LLMGateway().config.provider == LLMProvider.GEMINI

    # 7. Nvidia
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    assert LLMGateway().config.provider == LLMProvider.NVIDIA

    # Test _create_client for Gemini
    gemini_client = LLMGateway()._create_client(LLMConfig(provider=LLMProvider.GEMINI))
    assert isinstance(gemini_client, GeminiClient)


def test_llm_retry_delay_boundary_values():
    """Boundary values: _retry_delay with negative values, HTTP-date in past/future, non-finite values."""
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    # 1. Negative retry-after seconds -> falls back to exponential backoff
    r_neg = httpx.Response(429, headers={"retry-after": "-10"}, request=req)
    assert _retry_delay(r_neg, 0) == 0.5

    # 2. HTTP-date in the future (~10 seconds ahead)
    future_date = email.utils.formatdate(time.time() + 10, usegmt=True)
    r_date = httpx.Response(429, headers={"retry-after": future_date}, request=req)
    delay = _retry_delay(r_date, 0)
    assert 8.0 <= delay <= 12.0

    # 3. HTTP-date in the past -> delay clamped to 0.0
    past_date = email.utils.formatdate(time.time() - 100, usegmt=True)
    r_past = httpx.Response(429, headers={"retry-after": past_date}, request=req)
    assert _retry_delay(r_past, 0) == 0.0

    # 4. Invalid unparseable date/number -> falls back to backoff
    r_invalid = httpx.Response(429, headers={"retry-after": "not-a-number-or-date"}, request=req)
    assert _retry_delay(r_invalid, 1) == 1.0


@pytest.mark.asyncio
async def test_llm_fake_client_no_user_message_boundary():
    """Boundary value: FakeLLMClient.chat when message list contains no user turn."""
    client = FakeLLMClient(LLMConfig(provider=LLMProvider.FAKE))
    resp = await client.chat([ChatMessage(role="system", content="System only message")])
    assert "statutory rules and organizational policies" in resp.content


@pytest.mark.asyncio
async def test_llm_gemini_tool_response_types_boundary():
    """Boundary values: Gemini tool responses with dict vs scalar output vs unparseable string."""
    recorded_contents: List[Dict[str, Any]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        recorded_contents.extend(data.get("contents", []))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    client = GeminiClient(LLMConfig(provider=LLMProvider.GEMINI, api_key="k"), http_client=http)

    msgs = [
        ChatMessage(role="tool", content="42", name="scalar_tool"),
        ChatMessage(role="tool", content="plain unparseable string", name="string_tool"),
        ChatMessage(role="tool", content="{\"key\": \"val\"}", name="dict_tool"),
    ]
    await client.chat(msgs)

    # scalar 42 should become {"output": 42}
    part0 = recorded_contents[0]["parts"][0]["functionResponse"]["response"]
    assert part0 == {"output": 42}

    # plain string should become {"result": "plain unparseable string"}
    part1 = recorded_contents[1]["parts"][0]["functionResponse"]["response"]
    assert part1 == {"result": "plain unparseable string"}

    # dict should remain {"key": "val"}
    part2 = recorded_contents[2]["parts"][0]["functionResponse"]["response"]
    assert part2 == {"key": "val"}


def test_llm_anthropic_consecutive_tool_results_coalescing_boundary():
    """Boundary value: Consecutive tool result messages coalesced into a single user content array."""
    msgs = [
        ChatMessage(role="user", content="Calculate two tools"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="t-1", name="tool1", arguments={}),
                ToolCall(id="t-2", name="tool2", arguments={}),
            ],
        ),
        ChatMessage(role="tool", content="res1", tool_call_id="t-1"),
        ChatMessage(role="tool", content="res2", tool_call_id="t-2"),
    ]
    _, out = AnthropicClient._convert(msgs)
    # The two tool results must coalesce into out[2]["content"] list with 2 blocks
    assert out[2]["role"] == "user"
    assert len(out[2]["content"]) == 2
    assert out[2]["content"][0]["tool_use_id"] == "t-1"
    assert out[2]["content"][1]["tool_use_id"] == "t-2"


@pytest.mark.asyncio
async def test_llm_gateway_stream_generator_exit_boundary():
    """Boundary value: GeneratorExit during LLMGateway.chat_stream sets egress status ABORTED."""
    recorded_status: Optional[str] = None

    class MockEgressLog:
        def record(self, **kwargs) -> str:
            return "egress-gen-exit"

        def update(self, key: str, status: Optional[str] = None, **kwargs) -> None:
            nonlocal recorded_status
            if status is not None:
                recorded_status = status

    class InfiniteStreamClient(BaseLLMClient):
        async def chat(self, messages, tools=None, temperature=None):
            return LLMResponse(content="ok")

        async def chat_stream(self, messages, tools=None, **kwargs):
            for i in range(100):
                yield {"event": "token", "data": f"token-{i} "}

    gateway = LLMGateway(LLMConfig(provider=LLMProvider.FAKE), egress_log=MockEgressLog())
    gateway.client = InfiniteStreamClient(LLMConfig(provider=LLMProvider.FAKE))

    stream = gateway.chat_stream([ChatMessage(role="user", content="start")])
    # Read only 1 token and close generator early
    async for item in stream:
        assert "token" in item["event"]
        break
    await stream.aclose()

    assert recorded_status == "ABORTED"


@pytest.mark.asyncio
async def test_llm_openai_malformed_tool_call_arguments_negative():
    """Negative path: OpenAI tool call with non-JSON arguments falls back to {'raw': fn_args_raw}."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "func", "arguments": "{unclosed_json"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    client = OpenAICompatibleClient(LLMConfig(provider=LLMProvider.OPENAI, api_key="k"), http_client=http)
    resp = await client.chat([ChatMessage(role="user", content="test")])

    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"raw": "{unclosed_json"}


def test_llm_anthropic_invalid_effort_negative():
    """Negative path: AnthropicClient with unsupported effort level raises LLMNotConfigured."""
    with pytest.raises(LLMNotConfigured) as exc:
        AnthropicClient(LLMConfig(provider=LLMProvider.ANTHROPIC, api_key="k", effort="ultra_maximum"))
    assert "effort must be one of" in str(exc.value)


def test_llm_anthropic_unmatched_tool_id_negative():
    """Negative path: AnthropicClient._convert raises ValueError if tool result has no matching tool call."""
    msgs = [
        ChatMessage(role="user", content="Run tool"),
        ChatMessage(role="assistant", content="calling", tool_calls=[ToolCall(id="t-1", name="f", arguments={})]),
        ChatMessage(role="tool", content="res", tool_call_id="unmatched-t-999"),
    ]
    with pytest.raises(ValueError) as exc:
        AnthropicClient._convert(msgs)
    assert "tool result without a matching tool call" in str(exc.value)


def test_llm_anthropic_first_message_assistant_negative():
    """Negative path: AnthropicClient._convert raises ValueError if first message is from assistant."""
    msgs = [ChatMessage(role="assistant", content="I am answering prematurely")]
    with pytest.raises(ValueError) as exc:
        AnthropicClient._convert(msgs)
    assert "first message must be from the user" in str(exc.value)


def test_llm_gateway_policy_resolver_missing_negative():
    """Negative path: LLMGateway.policy_for without resolver for non-fake provider raises LlmNotAllowed."""
    gateway = LLMGateway(LLMConfig(provider=LLMProvider.OPENAI, api_key="k"), policy_resolver=None)
    with pytest.raises(LlmNotAllowed) as exc:
        gateway.policy_for("acme")
    assert "no LLM policy resolver configured" in str(exc.value)


def test_llm_gateway_budget_without_egress_log_negative():
    """Negative path: Daily budget with missing egress log raises LLMNotConfigured."""
    policy = LlmPolicy(allowed_providers=("fake",), max_data_tier=2, daily_token_budget=1000)
    gateway = LLMGateway(
        LLMConfig(provider=LLMProvider.FAKE),
        egress_log=None,
        policy_resolver=lambda tid, prov: policy,
    )
    with pytest.raises(LLMNotConfigured) as exc:
        gateway.preflight("acme")
    assert "a token budget needs the egress log" in str(exc.value)


def test_llm_gateway_budget_exceeded_negative():
    """Negative path: Exceeding daily token budget raises LlmBudgetExceeded."""
    policy = LlmPolicy(allowed_providers=("fake",), max_data_tier=2, daily_token_budget=100)

    class FullBudgetEgressLog:
        def tokens_today(self, tenant_id: str) -> int:
            return 150

    gateway = LLMGateway(
        LLMConfig(provider=LLMProvider.FAKE),
        egress_log=FullBudgetEgressLog(),
        policy_resolver=lambda tid, prov: policy,
    )
    with pytest.raises(LlmBudgetExceeded):
        gateway.preflight("acme")


@pytest.mark.asyncio
async def test_llm_gateway_pii_egress_blocked_negative(monkeypatch: pytest.MonkeyPatch):
    """Negative path: Messages or tool arguments containing unredacted PII raise PiiEgressBlocked."""
    # Simulate a redaction bypass or failure where unredacted PII reaches safety assertion
    from hrms_plugin.security.pii import PiiSession
    monkeypatch.setattr(PiiSession, "redact_text", lambda self, text: text)

    gateway = LLMGateway(LLMConfig(provider=LLMProvider.FAKE))
    leaky_message = ChatMessage(role="user", content="My Aadhaar is 234567890124 please verify")
    with pytest.raises(PiiEgressBlocked):
        await gateway.chat([leaky_message])


def test_llm_gateway_unknown_or_missing_provider_env_negative(monkeypatch: pytest.MonkeyPatch):
    """Negative path: Unknown LLM_PROVIDER or completely unconfigured env raises LLMNotConfigured."""
    monkeypatch.setenv("LLM_PROVIDER", "unsupported_unknown_vendor")
    with pytest.raises(LLMNotConfigured) as exc1:
        LLMGateway()._resolve_config_from_env()
    assert "unknown LLM_PROVIDER" in str(exc1.value)

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    env_keys = (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "NVIDIA_API_KEY",
    )
    for k in env_keys:
        monkeypatch.delenv(k, raising=False)

    with pytest.raises(LLMNotConfigured) as exc2:
        LLMGateway()._resolve_config_from_env()
    assert "no LLM provider configured" in str(exc2.value)


@pytest.mark.asyncio
async def test_llm_gateway_chat_and_stream_client_exception_negative():
    """Negative path: Underlying client failure during chat or stream records FAILED status in egress."""
    egress_statuses: List[str] = []

    class MockEgressLog:
        def record(self, **kwargs) -> str:
            return "egress-err"

        def update(self, key: str, status: Optional[str] = None, **kwargs) -> None:
            if status is not None:
                egress_statuses.append(status)

    class FailingClient(BaseLLMClient):
        async def chat(self, messages, tools=None, temperature=None):
            raise RuntimeError("Underlying network crash")

        async def chat_stream(self, messages, tools=None, **kwargs):
            raise RuntimeError("Stream connection dropped")
            if False:
                yield {}

    gateway = LLMGateway(LLMConfig(provider=LLMProvider.FAKE), egress_log=MockEgressLog())
    gateway.client = FailingClient(LLMConfig(provider=LLMProvider.FAKE))

    # 1. Chat error
    with pytest.raises(RuntimeError):
        await gateway.chat([ChatMessage(role="user", content="test")])
    assert "FAILED" in egress_statuses

    # 2. Stream error
    with pytest.raises(RuntimeError):
        async for _ in gateway.chat_stream([ChatMessage(role="user", content="test")]):
            pass
    assert egress_statuses.count("FAILED") >= 2


# ====================================================================================================
# BEHAVIOUR 3: FrappeClient host interaction, document retrieval/patching, and submitted slip contract
# ====================================================================================================

def _make_frappe_host(tenant_id: str = "acme") -> HostConfig:
    return HostConfig(
        tenant_id=tenant_id,
        vendor="frappe",
        base_url="https://frappe.test",
        api_key_env="K",
        api_secret_env="S",
        timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_frappe_client_aclose_owned_http_positive():
    """Positive path: FrappeClient aclose cleanly closes owned http client when none passed."""
    client = FrappeClient(_make_frappe_host(), "api-key", "api-secret")
    assert client._owns_http is True
    await client.aclose()


@pytest.mark.asyncio
async def test_frappe_client_get_and_update_document_positive():
    """Positive path: FrappeClient get_document and update_document with dict payload."""
    recorded_calls: List[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_calls.append(f"{request.method} {request.url.path}")
        assert request.headers.get("Authorization") == "token my-key:my-secret"
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"name": "EMP-001", "employee_name": "Satoshi"}})
        elif request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": {"name": "EMP-001", **body}})
        return httpx.Response(405)

    client = FrappeClient(
        _make_frappe_host(),
        "my-key",
        "my-secret",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    doc = await client.get_document("Employee", "EMP-001")
    assert doc["employee_name"] == "Satoshi"

    updated = await client.update_document("Employee", "EMP-001", {"notice_number_of_days": 60})
    assert updated["notice_number_of_days"] == 60
    assert len(recorded_calls) == 2


@pytest.mark.asyncio
async def test_frappe_client_fetch_submitted_salary_slip_positive():
    """Positive path: fetch_submitted_salary_slip finds single submitted slip and fetches details."""
    slip_data = {
        "name": "SAL-SLIP-2026-001",
        "employee": "EMP-101",
        "start_date": "2026-05-01",
        "docstatus": 1,
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "limit_page_length" in str(request.url):
            return httpx.Response(200, json={"data": [{"name": "SAL-SLIP-2026-001"}]})
        elif "SAL-SLIP-2026-001" in request.url.path:
            return httpx.Response(200, json={"data": slip_data})
        return httpx.Response(404)

    client = FrappeClient(
        _make_frappe_host(),
        "k",
        "s",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    slip = await client.fetch_submitted_salary_slip("EMP-101", "2026-05")
    assert slip["name"] == "SAL-SLIP-2026-001"
    assert slip["docstatus"] == 1


@pytest.mark.asyncio
async def test_frappe_client_url_encoding_special_chars_boundary():
    """Boundary value: URL quoting for doctype and names with spaces, slashes, and special characters."""
    raw_path_bytes: bytes = b""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal raw_path_bytes
        raw_path_bytes = request.url.raw_path
        return httpx.Response(200, json={"data": {"name": "DOC/2026 01"}})

    client = FrappeClient(
        _make_frappe_host(),
        "k",
        "s",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    await client.get_document("Salary Slip", "DOC/2026 01")
    assert raw_path_bytes == b"/api/resource/Salary%20Slip/DOC%2F2026%2001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupted_data",
    [
        "string instead of dict",
        [1, 2, 3],
        None,
        42,
    ],
)
async def test_frappe_client_non_dict_document_response_negative(corrupted_data: Any):
    """Negative path: get_document and update_document raise HOST_ERROR when response data is not an object."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": corrupted_data})

    client = FrappeClient(
        _make_frappe_host(),
        "k",
        "s",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(FrappeError) as exc_get:
        await client.get_document("Employee", "EMP-001")
    assert exc_get.value.code == "HOST_ERROR"
    assert "document is not an object" in exc_get.value.detail

    with pytest.raises(FrappeError) as exc_put:
        await client.update_document("Employee", "EMP-001", {"field": "val"})
    assert exc_put.value.code == "HOST_ERROR"
    assert "document is not an object" in exc_put.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing_data",
    [
        [{"name": ""}],  # empty name
        [{"name": None}],  # null name
        [{"other_key": "val"}],  # missing name key
        ["non-dict-item"],  # non-dict entry
    ],
)
async def test_frappe_client_fetch_submitted_slip_entry_without_name_negative(listing_data: List[Any]):
    """Negative path: fetch_submitted_salary_slip raises HOST_ERROR when listing entry has no valid name."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": listing_data})

    client = FrappeClient(
        _make_frappe_host(),
        "k",
        "s",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(FrappeError) as exc:
        await client.fetch_submitted_salary_slip("EMP-001", "2026-05")
    assert exc.value.code == "HOST_ERROR"
    assert "listing entry without a name" in exc.value.detail


@pytest.mark.asyncio
async def test_frappe_client_fetch_submitted_slip_second_call_non_dict_negative():
    """Negative path: fetch_submitted_salary_slip raises HOST_ERROR when second document call returns non-dict."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "limit_page_length" in str(request.url):
            return httpx.Response(200, json={"data": [{"name": "SLIP-VALID-NAME"}]})
        # Second call returns non-dict data
        return httpx.Response(200, json={"data": ["not", "an", "object"]})

    client = FrappeClient(
        _make_frappe_host(),
        "k",
        "s",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)),
    )

    with pytest.raises(FrappeError) as exc:
        await client.fetch_submitted_salary_slip("EMP-001", "2026-05")
    assert exc.value.code == "HOST_ERROR"
    assert "document is not an object" in exc.value.detail


# ====================================================================================================
# BEHAVIOUR 4: WritebackService two-phase lifecycle, host errors, and rollback invariants
# ====================================================================================================

def test_writeback_same_decimal_numerical_equality_positive():
    """Positive path: _same compares decimal representations numerically."""
    assert _same(1500.5, "1500.50", "decimal") is True
    assert _same("1500.5000", "1500.5", "decimal") is True
    assert _same(1500, "1500.00", "decimal") is True
    assert _same(1500, "1501", "decimal") is False


def test_writeback_valid_string_types_positive():
    """Positive path: _valid correctly accepts string lengths <= 140."""
    assert _valid("Short text", "string") is True
    assert _valid("", "string") is True


def test_writeback_boundaries_same_and_valid():
    """Boundary values: _same decimal types (bool, non-numeric, invalid decimal) and string length 140."""
    # Line 37: bool host_value -> False
    assert _same(True, "1", "decimal") is False
    assert _same(False, "0", "decimal") is False

    # Line 37: non-numeric container host_value -> False
    assert _same([100], "100", "decimal") is False
    assert _same({"val": 100}, "100", "decimal") is False

    # Lines 40-41: unparseable decimal string -> False
    assert _same("invalid_decimal_digits", "100", "decimal") is False

    # Line 49: exactly 140 chars -> True; 141 chars -> False
    assert _valid("a" * 140, "string") is True
    assert _valid("a" * 141, "string") is False
    assert _valid(12345, "string") is False

    # Line 65: unknown kind -> False
    assert _valid("hello", "unsupported_data_type") is False


@pytest.mark.asyncio
async def test_writeback_host_not_configured_unknown_tenant_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Negative path: Proposing write-back on unconfigured tenant raises HOST_NOT_CONFIGURED (line 87)."""
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {
                        "tenant_id": "acme",
                        "vendor": "frappe",
                        "base_url": "https://hr.test",
                        "api_key_env": "K",
                        "api_secret_env": "S",
                        "timeout_seconds": 5,
                        "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("K", "key")
    monkeypatch.setenv("S", "secret")

    svc = WritebackService(store=MemoryKeyValueStore())
    auth = AuthContext(user_id="u1", tenant_id="unknown_tenant", roles=frozenset({UserRole.HR_ADMIN}))

    with pytest.raises(WritebackError) as exc:
        await svc.propose(auth, "Employee", "EMP-001", {"notice_number_of_days": 30})
    assert exc.value.code == "HOST_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_writeback_host_registry_error_credentials_missing_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Negative path: Missing host credentials environment variable raises HOST_NOT_CONFIGURED."""
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {
                        "tenant_id": "acme",
                        "vendor": "frappe",
                        "base_url": "https://hr.test",
                        "api_key_env": "MISSING_ENV_KEY",
                        "api_secret_env": "MISSING_ENV_SECRET",
                        "timeout_seconds": 5,
                        "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.delenv("MISSING_ENV_KEY", raising=False)
    monkeypatch.delenv("MISSING_ENV_SECRET", raising=False)

    svc = WritebackService(store=MemoryKeyValueStore())
    auth = AuthContext(user_id="u1", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))

    with pytest.raises(WritebackError) as exc:
        await svc.propose(auth, "Employee", "EMP-001", {"notice_number_of_days": 30})
    assert exc.value.code == "HOST_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_writeback_document_missing_modified_timestamp_negative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Negative path: Host document missing modified timestamp raises HOST_ERROR."""
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {
                        "tenant_id": "acme",
                        "vendor": "frappe",
                        "base_url": "https://hr.test",
                        "api_key_env": "K",
                        "api_secret_env": "S",
                        "timeout_seconds": 5,
                        "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("K", "key")
    monkeypatch.setenv("S", "secret")

    def mock_factory(host, k, s):
        def handler(request: httpx.Request) -> httpx.Response:
            # Document missing "modified" timestamp
            return httpx.Response(200, json={"data": {"name": "EMP-001", "notice_number_of_days": 30}})

        return FrappeClient(host, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    svc = WritebackService(store=MemoryKeyValueStore(), client_factory=mock_factory)
    auth = AuthContext(user_id="u1", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))

    with pytest.raises(WritebackError) as exc:
        await svc.propose(auth, "Employee", "EMP-001", {"notice_number_of_days": 45})
    assert exc.value.code == "HOST_ERROR"
    assert "document has no modified timestamp" in exc.value.detail


@pytest.mark.asyncio
async def test_writeback_approve_reread_failure_and_rollback_unavailable_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Negative path: When reread after approval fails, after_modified is None, making rollback unavailable."""
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {
                        "tenant_id": "acme",
                        "vendor": "frappe",
                        "base_url": "https://hr.test",
                        "api_key_env": "K",
                        "api_secret_env": "S",
                        "timeout_seconds": 5,
                        "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("K", "key")
    monkeypatch.setenv("S", "secret")

    call_count = 0

    def mock_factory(host, k, s):
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.method == "GET" and call_count == 1:
                # 1. First get in propose
                return httpx.Response(
                    200,
                    json={"data": {"name": "EMP-001", "notice_number_of_days": 30, "modified": "2026-10-01 10:00:00"}},
                )
            elif request.method == "GET" and call_count == 2:
                # 2. Pre-check in approve
                return httpx.Response(
                    200,
                    json={"data": {"name": "EMP-001", "notice_number_of_days": 30, "modified": "2026-10-01 10:00:00"}},
                )
            elif request.method == "PUT":
                # 3. Update document in approve succeeds but host omits modified in response
                return httpx.Response(200, json={"data": {"name": "EMP-001", "notice_number_of_days": 45}})
            elif request.method == "GET" and call_count >= 4:
                # 4. Post-write reread fails with 500 error -> lines 190-191 set after_modified = None
                return httpx.Response(500, json={"error": "reread failed"})
            return httpx.Response(404)

        return FrappeClient(host, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    svc = WritebackService(store=MemoryKeyValueStore(), client_factory=mock_factory)
    proposer = AuthContext(user_id="hr-proposer", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
    approver = AuthContext(user_id="hr-approver", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))

    prop = await svc.propose(proposer, "Employee", "EMP-001", {"notice_number_of_days": 45})
    proposal_id = prop["id"]

    # Approval succeeds even though reread fails; after_modified becomes None
    approved = await svc.approve(approver, proposal_id)
    assert approved["status"] == "EXECUTED"
    assert approved["after_modified"] is None

    # Line 204: Rollback without after_modified raises ROLLBACK_UNAVAILABLE
    with pytest.raises(WritebackError) as exc_rb:
        await svc.rollback(approver, proposal_id)
    assert exc_rb.value.code == "ROLLBACK_UNAVAILABLE"
    assert "did not report the post-write version" in exc_rb.value.detail


# ====================================================================================================
# BEHAVIOUR 5: Statutory PF payroll calculation, slip fingerprinting, and rule validation contracts
# ====================================================================================================

def test_fingerprint_supported_json_types_positive():
    """Positive path: slip_fingerprint generates consistent deterministic sha256 hash."""
    slip = {
        "name": "SLIP-01",
        "employee": "EMP-01",
        "start_date": "2026-05-01",
        "end_date": "2026-05-31",
        "currency": "INR",
        "gross_pay": 50000.0,
        "docstatus": 1,
        "modified": "2026-06-01 00:00:00",
        "earnings": [
            {"salary_component": "Basic", "abbr": "BASIC", "amount": 25000.0},
        ],
    }
    fp1 = slip_fingerprint(slip)
    fp2 = slip_fingerprint(slip)
    assert fp1 == fp2
    assert fp1.startswith("sha256:")


def test_fingerprint_bool_object_custom_type_boundaries():
    """Boundary values: _json_type handles bool, dict (object), and custom type fallback."""
    assert _json_type(True) == "bool"
    assert _json_type(False) == "bool"
    assert _json_type({"nested": "dict"}) == "object"
    assert _json_type((1, 2, 3)) == "tuple"
    assert _json_type({1, 2}) == "set"

    # Slip containing bool, dict, and tuple types in top-level and earnings row
    slip = {
        "name": "SLIP-02",
        "docstatus": True,
        "earnings": [
            {"abbr": "B", "amount": 1000, "custom_dict": {"x": 1}, "custom_tuple": (1, 2)},
        ],
    }
    fp = slip_fingerprint(slip)
    assert fp.startswith("sha256:")


def test_rules_valid_payroll_rules_positive(tmp_path: Path):
    """Positive path: PfRules and load_payroll_rules load and validate statutory parameters."""
    rules_content = """
tenants:
  - tenant_id: "acme"
    rules_version: "2026-statutory-v1"
    currency: "INR"
    pf:
      required_components: ["BASIC"]
      optional_components: ["DA"]
      employee_rate: "0.12"
      employer_rate: "0.12"
      eps_rate: "0.0833"
      wage_ceiling: "15000"
      eps_wage_ceiling: "15000"
      apply_ceiling_to_employee: true
      apply_ceiling_to_employer: true
"""
    rules_file = tmp_path / "valid_rules.yaml"
    rules_file.write_text(rules_content)

    registry = load_payroll_rules(rules_file)
    tenant_rules = registry.for_tenant("acme")
    assert tenant_rules is not None
    assert tenant_rules.pf.wage_ceiling == Decimal("15000")
    assert tenant_rules.pf.employee_rate == Decimal("0.12")


def test_rules_ceiling_non_string_negative():
    """Negative path: Non-string integer or float ceiling raises ValueError (line 63)."""
    with pytest.raises(ValidationError) as exc:
        PfRules(
            required_components=("BASIC",),
            optional_components=(),
            employee_rate="0.12",
            employer_rate="0.12",
            eps_rate="0.0833",
            wage_ceiling=15000,  # int instead of decimal string "15000"
            eps_wage_ceiling="15000",
            apply_ceiling_to_employee=True,
            apply_ceiling_to_employer=True,
        )
    assert "ceilings must be decimal strings" in str(exc.value)


def test_rules_registry_duplicate_tenant_negative():
    """Negative path: PayrollRulesRegistry with duplicate tenant_id raises ValueError (line 100)."""
    pf = PfRules(
        required_components=("BASIC",),
        optional_components=(),
        employee_rate="0.12",
        employer_rate="0.12",
        eps_rate="0.0833",
        wage_ceiling="15000",
        eps_wage_ceiling="15000",
        apply_ceiling_to_employee=True,
        apply_ceiling_to_employer=True,
    )
    t1 = TenantPayrollRules(tenant_id="dup_tenant", rules_version="1", currency="INR", pf=pf)
    t2 = TenantPayrollRules(tenant_id="dup_tenant", rules_version="2", currency="INR", pf=pf)

    with pytest.raises(ValidationError) as exc:
        PayrollRulesRegistry(tenants=(t1, t2))
    assert "duplicate tenant_id in payroll rules" in str(exc.value)


def test_load_payroll_rules_invalid_yaml_negative(tmp_path: Path):
    """Negative path: load_payroll_rules raises RulesError on invalid YAML syntax (lines 112-113)."""
    bad_yaml = tmp_path / "invalid.yaml"
    bad_yaml.write_text("tenants:\n  - unclosed_mapping: [")

    with pytest.raises(RulesError) as exc:
        load_payroll_rules(bad_yaml)
    assert "invalid YAML in payroll rules" in str(exc.value)


def test_compute_pf_earnings_missing_or_not_list_negative():
    """Negative path: compute_pf raises INCONSISTENT_PAYLOAD when earnings is missing or not a list (line 40)."""
    pf = PfRules(
        required_components=("BASIC",),
        optional_components=(),
        employee_rate="0.12",
        employer_rate="0.12",
        eps_rate="0.0833",
        wage_ceiling="15000",
        eps_wage_ceiling="15000",
        apply_ceiling_to_employee=True,
        apply_ceiling_to_employer=True,
    )
    tr = TenantPayrollRules(tenant_id="acme", rules_version="1", currency="INR", pf=pf)

    with pytest.raises(PfError) as exc_none:
        compute_pf({"currency": "INR", "earnings": None}, tr)
    assert exc_none.value.code == "INCONSISTENT_PAYLOAD"
    assert "earnings missing" in exc_none.value.detail

    with pytest.raises(PfError) as exc_str:
        compute_pf({"currency": "INR", "earnings": "not-a-list"}, tr)
    assert exc_str.value.code == "INCONSISTENT_PAYLOAD"
    assert "earnings missing" in exc_str.value.detail


def test_compute_pf_earnings_row_abbr_missing_negative():
    """Negative path: compute_pf raises INCONSISTENT_PAYLOAD when row abbr is missing or empty (line 47)."""
    pf = PfRules(
        required_components=("BASIC",),
        optional_components=(),
        employee_rate="0.12",
        employer_rate="0.12",
        eps_rate="0.0833",
        wage_ceiling="15000",
        eps_wage_ceiling="15000",
        apply_ceiling_to_employee=True,
        apply_ceiling_to_employer=True,
    )
    tr = TenantPayrollRules(tenant_id="acme", rules_version="1", currency="INR", pf=pf)

    slip_no_abbr = {"currency": "INR", "earnings": [{"amount": 1000}]}
    with pytest.raises(PfError) as exc_missing:
        compute_pf(slip_no_abbr, tr)
    assert exc_missing.value.code == "INCONSISTENT_PAYLOAD"
    assert "earnings[0].abbr missing" in exc_missing.value.detail

    slip_empty_abbr = {"currency": "INR", "earnings": [{"abbr": "", "amount": 1000}]}
    with pytest.raises(PfError) as exc_empty:
        compute_pf(slip_empty_abbr, tr)
    assert exc_empty.value.code == "INCONSISTENT_PAYLOAD"
    assert "earnings[0].abbr missing" in exc_empty.value.detail


# ====================================================================================================
# BEHAVIOUR 6: Key-Value storage backends (Sqlite, Memory, Mapping) TTL expiration and corruption
# ====================================================================================================

def test_sqlite_store_purge_all_namespaces_positive(tmp_path: Path):
    """Positive path: _purge_expired with namespace=None purges expired keys across all namespaces."""
    db_file = tmp_path / "test_store.db"
    store = SqliteKeyValueStore(str(db_file))

    # Insert expired entries in two different namespaces
    store.set("ns1", "expired_key_1", {"v": 1}, ttl_seconds=-10)
    store.set("ns2", "expired_key_2", {"v": 2}, ttl_seconds=-10)
    store.set("ns1", "live_key_1", {"v": 3}, ttl_seconds=3600)

    with store._lock, store._get_connection() as conn:
        store._purge_expired(conn, namespace=None)  # Line 68

    assert store.get("ns1", "expired_key_1") is None
    assert store.get("ns2", "expired_key_2") is None
    assert store.get("ns1", "live_key_1") == {"v": 3}


def test_sqlite_store_clear_all_and_close_positive(tmp_path: Path):
    """Positive path: SqliteKeyValueStore clear(None) clears all entries; close() runs cleanly."""
    db_file = tmp_path / "test_clear.db"
    store = SqliteKeyValueStore(str(db_file))

    store.set("ns1", "k1", {"a": 1})
    store.set("ns2", "k2", {"b": 2})

    store.clear(namespace=None)  # Line 166
    assert store.get("ns1", "k1") is None
    assert store.get("ns2", "k2") is None

    store.close()  # Line 170


def test_sqlite_store_lazy_get_expired_negative(tmp_path: Path):
    """Negative path: SqliteKeyValueStore.get lazily deletes expired item and returns None (lines 88-93)."""
    db_file = tmp_path / "test_lazy.db"
    store = SqliteKeyValueStore(str(db_file))

    store.set("ns", "k_expired", {"test": True}, ttl_seconds=-5)
    # Directly fetch row to confirm it exists in DB with expired timestamp
    with store._lock, store._get_connection() as conn:
        cursor = conn.execute("SELECT count(*) FROM kv_store WHERE namespace='ns' AND key='k_expired'")
        assert cursor.fetchone()[0] == 1

    # Calling get() triggers lazy deletion at lines 88-93
    assert store.get("ns", "k_expired") is None

    # Confirm row was purged from sqlite table
    with store._lock, store._get_connection() as conn:
        cursor = conn.execute("SELECT count(*) FROM kv_store WHERE namespace='ns' AND key='k_expired'")
        assert cursor.fetchone()[0] == 0


def test_sqlite_store_corrupted_json_in_get_and_get_all_negative(tmp_path: Path):
    """Negative path: SqliteKeyValueStore gracefully returns None for get and skips in get_all on corrupted JSON."""
    db_file = tmp_path / "test_corrupt.db"
    store = SqliteKeyValueStore(str(db_file))

    # Manually inject corrupted JSON into sqlite
    with store._lock, store._get_connection() as conn:
        sql = "INSERT INTO kv_store (namespace, key, value, updated_at) VALUES ('ns', 'corrupted_k', '{bad json', 1)"
        conn.execute(sql)
        conn.commit()

    # Lines 96-97: get() catches json.loads exception and returns None
    assert store.get("ns", "corrupted_k") is None

    # Lines 157-158: get_all() skips the corrupted row safely
    all_rows = store.get_all("ns")
    assert "corrupted_k" not in all_rows


def test_memory_store_lifecycle_and_clear_namespace_positive():
    """Positive path: MemoryKeyValueStore clear(namespace) isolates clearing to specified namespace."""
    mem = MemoryKeyValueStore()
    mem.set("ns1", "k1", {"val": 10})
    mem.set("ns2", "k2", {"val": 20})

    mem.clear(namespace="ns1")  # Lines 64-65
    assert mem.get("ns1", "k1") is None
    assert mem.get("ns2", "k2") == {"val": 20}

    mem.clear(namespace=None)  # Lines 66-67
    assert mem.get("ns2", "k2") is None
    mem.close()  # Line 70


def test_memory_store_delete_nonexistent_boundary():
    """Boundary value: MemoryKeyValueStore.delete returns False when namespace or key does not exist (line 52)."""
    mem = MemoryKeyValueStore()
    assert mem.delete("nonexistent_namespace", "nonexistent_key") is False

    mem.set("existing_ns", "key1", {"x": 1})
    assert mem.delete("existing_ns", "key_other") is False


def test_memory_store_lazy_get_expired_boundary(monkeypatch: pytest.MonkeyPatch):
    """Boundary value: MemoryKeyValueStore.get lazily deletes expired entry (lines 38-39)."""
    mem = MemoryKeyValueStore()
    mem._data["ns"] = {"exp_key": ({"v": 1}, 1001.0)}

    times = [1000.0, 1002.0]
    import hrms_plugin.storage.memory_store as mem_mod
    monkeypatch.setattr(mem_mod.time, "time", lambda: times.pop(0) if times else 1002.0)

    assert mem.get("ns", "exp_key") is None  # Lines 38-39
    assert "exp_key" not in mem._data["ns"]


def test_mapping_store_activate_and_clear_positive():
    """Positive path: MappingStore._activate persists mapping and clear() wipes both namespaces."""
    mem_store = MemoryKeyValueStore()
    ms = MappingStore(backend=mem_store)

    mapping = EntityMapping(
        vendor="frappe",
        entity_type="EMPLOYEE",
        id_path="name",
        version_path="modified",
    )
    ms._activate("acme", mapping)  # Lines 98-100

    loaded = ms.load_mapping("acme", "frappe", EntityType.EMPLOYEE)
    assert loaded is not None
    assert loaded.id_path == "name"

    ms.clear()  # Lines 131-132
    assert ms.load_mapping("acme", "frappe", EntityType.EMPLOYEE) is None


def test_mapping_store_list_mappings_prefix_and_parts_boundaries():
    """Boundary values: MappingStore.list_mappings filters non-matching prefixes and malformed keys."""
    mem_store = MemoryKeyValueStore()
    ms = MappingStore(backend=mem_store)

    # Key from different tenant (branch 124->123)
    mem_store.set(ms.NAMESPACE, "initech:frappe:employee", {"vendor": "frappe"})
    # Key with fewer than 3 parts (branch 126->123)
    mem_store.set(ms.NAMESPACE, "acme:onlytwoparts", {"vendor": "frappe"})
    # Valid key with >= 3 parts
    mem_store.set(ms.NAMESPACE, "acme:frappe:employee", {"vendor": "frappe"})

    results = ms.list_mappings("acme")
    assert results == [{"vendor": "frappe", "entity_type": "employee"}]


def test_mapping_store_load_corrupted_mapping_negative():
    """Negative path: MappingStore.load_mapping catches exception on invalid mapping dict (lines 115-116)."""
    mem_store = MemoryKeyValueStore()
    ms = MappingStore(backend=mem_store)

    # Store a dict with valid make_key but invalid mapping content
    key = ms._make_key("acme", "frappe", EntityType.EMPLOYEE)
    mem_store.set(ms.NAMESPACE, key, {"invalid": "corrupted mapping structure"})
    loaded = ms.load_mapping("acme", "frappe", EntityType.EMPLOYEE)  # Lines 115-116
    assert loaded is None
