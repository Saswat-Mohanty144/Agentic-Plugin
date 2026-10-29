"""VOICE-1: without a Sarvam key the voice routes fail closed (503) instead of answering with canned data.

Before the fix, the production VoiceAgent() had no key and silently returned a fixed transcript ("Apply sick leave for
tomorrow due to fever"), which /v1/voice/turn then ran through the real agent as if the employee had said it.
"""

import base64

import pytest

from hrms_plugin.agents.voice import VoiceAgent, VoiceUpstreamError
from hrms_plugin.server import app as app_module

AUDIO = base64.b64encode(b"valid_audio_bytes").decode()
ROUTES = [("/v1/voice/transcribe", {"audio_base64": AUDIO}), ("/v1/voice/synthesize", {"text": "hello"}),
          ("/v1/voice/turn", {"audio_base64": AUDIO})]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, ""])
async def test_agent_without_key_refuses(key):
    agent = VoiceAgent(api_key=key)
    with pytest.raises(VoiceUpstreamError) as stt:
        await agent.transcribe_audio(b"x")
    with pytest.raises(VoiceUpstreamError) as tts:
        await agent.synthesize_speech("hello")
    assert stt.value.code == tts.value.code == "VOICE_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_offline_mock_must_be_requested_explicitly():
    agent = VoiceAgent(offline_mock=True)
    assert (await agent.transcribe_audio(b"x"))["mocked"] is True
    assert (await agent.synthesize_speech("hello"))["mocked"] is True


def test_production_agent_is_not_a_mock():
    assert app_module.voice_agent.offline_mock is False


@pytest.mark.parametrize("route, body", ROUTES)
def test_routes_without_key_are_503(make_client, bearer, monkeypatch, route, body):
    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent())
    res = make_client(raise_server_exceptions=False).post(route, json=body, headers=bearer(roles=["staff"]))
    assert res.status_code == 503 and res.json() == {"error": "VOICE_NOT_CONFIGURED"}


def test_turn_without_key_never_reaches_the_agent(make_client, bearer, monkeypatch):
    calls = []

    class Spy:
        async def process_agentic(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent())
    res = make_client(supervisor_agent=Spy(), raise_server_exceptions=False).post(
        "/v1/voice/turn", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    assert res.status_code == 503 and calls == []
