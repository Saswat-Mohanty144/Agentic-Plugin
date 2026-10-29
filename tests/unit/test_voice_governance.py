"""VOICE-2: voice routes run under the tenant data policy, the egress log and PII protection (owner, 2026-10-29).

V1 opt-in per tenant (llm_policy.voice_providers, empty by default); V2 every real Sarvam call is logged before it
leaves; V3 text-to-speech never sends detectable PII (synthesize 422, turn withholds only the audio); V4 audio cannot
be redacted, so tenant opt-in is the gate; V5 key from SARVAM_API_KEY, absent -> 503.
"""

import base64
import hashlib

import httpx
import pytest
from pydantic import ValidationError

from hrms_plugin.agents.supervisor import AgentResponse, UserIntent
from hrms_plugin.agents.voice import VoiceAgent
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.memory_store import MemoryKeyValueStore

RAW = b"valid_audio_bytes"
AUDIO = base64.b64encode(RAW).decode()
ROUTES = [("/v1/voice/transcribe", {"audio_base64": AUDIO}), ("/v1/voice/synthesize", {"text": "hello"}),
          ("/v1/voice/turn", {"audio_base64": AUDIO})]
PII_TEXTS = ["Mail ravi@acme.test", "Call +91 98765 43210", "PAN ABCPE1234F"]


class Sarvam:
    """Mock Sarvam: records each outbound request."""

    def __init__(self, monkeypatch, status=200):
        self.requests = []
        self.status = status
        original = httpx.AsyncClient
        monkeypatch.setattr("hrms_plugin.agents.voice.httpx.AsyncClient",
                            lambda *a, **kw: original(*a, **{**kw, "transport": httpx.MockTransport(self._handle)}))
        monkeypatch.setattr(app_module, "voice_agent", VoiceAgent(api_key="sarvam-key"))

    def _handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("speech-to-text"):
            return httpx.Response(self.status, json={"transcript": "leave balance", "language_code": "en-IN"})
        return httpx.Response(self.status, json={"audios": ["UklGRg=="]})


class Supervisor:
    def __init__(self, reply="You have 12 days left."):
        self.reply = reply

    async def process_agentic(self, **kw):
        return AgentResponse(intent=UserIntent.LEAVE_APPLY, routed_agent="leave", reply_text=self.reply,
                             confidence_score=1.0)


@pytest.fixture
def log(monkeypatch):
    egress = EgressLog(MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "egress_log", egress)
    return egress


# V1 -------------------------------------------------------------------------------------------------------------
def test_policy_voice_providers_default_empty_and_closed_set():
    assert LlmPolicy(allowed_providers=("fake",)).voice_providers == ()
    assert LlmPolicy(allowed_providers=("fake",), voice_providers=("sarvam",)).voice_providers == ("sarvam",)
    for bad in (("openai",), ("SARVAM",), "sarvam", ("",)):
        with pytest.raises(ValidationError):
            LlmPolicy(allowed_providers=("fake",), voice_providers=bad)


@pytest.mark.parametrize("route, body", ROUTES)
def test_tenant_without_voice_opt_in_is_403_and_nothing_leaves(make_client, bearer, monkeypatch, log, route, body):
    sarvam = Sarvam(monkeypatch)
    res = make_client(supervisor_agent=Supervisor()).post(
        route, json=body, headers=bearer(roles=["staff"], tenant="globex"))
    assert res.status_code == 403 and res.json() == {"error": "VOICE_NOT_ALLOWED_FOR_TENANT"}
    assert sarvam.requests == [] and log.list("globex") == []


# V2 -------------------------------------------------------------------------------------------------------------
def test_transcribe_is_logged_with_audio_hash(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch)
    res = make_client().post("/v1/voice/transcribe", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    assert res.status_code == 200
    [row] = log.list("acme")
    assert row["provider"] == "sarvam" and row["model"] == "saaras:v2" and row["status"] == "COMPLETED"
    assert row["payload_sha256"] == "sha256:" + hashlib.sha256(RAW).hexdigest()
    assert row["redacted_kinds"] == {}


def test_synthesize_is_logged_with_text_hash(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch)
    res = make_client().post("/v1/voice/synthesize", json={"text": "hello"}, headers=bearer(roles=["staff"]))
    assert res.status_code == 200
    [row] = log.list("acme")
    assert row["model"] == "bulbul:v1" and row["status"] == "COMPLETED"
    assert row["payload_sha256"] == "sha256:" + hashlib.sha256(b"hello").hexdigest()


def test_turn_logs_both_calls(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch)
    res = make_client(supervisor_agent=Supervisor()).post(
        "/v1/voice/turn", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    assert res.status_code == 200
    assert sorted(r["model"] for r in log.list("acme")) == ["bulbul:v1", "saaras:v2"]


def test_upstream_failure_is_logged_failed(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch, status=500)
    res = make_client(raise_server_exceptions=False).post(
        "/v1/voice/transcribe", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    assert res.status_code == 502
    [row] = log.list("acme")
    assert row["status"] == "FAILED"


def test_egress_is_visible_on_the_governance_route(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch)
    client = make_client()
    client.post("/v1/voice/synthesize", json={"text": "hello"}, headers=bearer(roles=["staff"]))
    rows = client.get("/v1/governance/llm-egress", headers=bearer(roles=["hr_manager"])).json()["records"]
    assert [r["provider"] for r in rows] == ["sarvam"]


@pytest.mark.parametrize("route, body", ROUTES)
def test_nothing_logged_when_nothing_is_sent(make_client, bearer, monkeypatch, log, route, body):
    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent())  # no key -> 503
    res = make_client(supervisor_agent=Supervisor(), raise_server_exceptions=False).post(
        route, json=body, headers=bearer(roles=["staff"]))
    assert res.status_code == 503 and log.list("acme") == []
    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent(offline_mock=True))
    res = make_client(supervisor_agent=Supervisor()).post(route, json=body, headers=bearer(roles=["staff"]))
    assert res.status_code == 200 and log.list("acme") == []


# V3 -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", PII_TEXTS)
def test_synthesize_never_sends_pii(make_client, bearer, monkeypatch, log, text):
    sarvam = Sarvam(monkeypatch)
    res = make_client().post("/v1/voice/synthesize", json={"text": text}, headers=bearer(roles=["staff"]))
    assert res.status_code == 422 and res.json()["error"] == "PII_EGRESS_BLOCKED"
    assert sarvam.requests == [] and log.list("acme") == []


@pytest.mark.parametrize("text", PII_TEXTS)
def test_turn_withholds_audio_but_keeps_the_answer_when_reply_has_pii(make_client, bearer, monkeypatch, log, text):
    sarvam = Sarvam(monkeypatch)
    res = make_client(supervisor_agent=Supervisor(reply=text)).post(
        "/v1/voice/turn", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    body = res.json()
    assert res.status_code == 200 and body["reply_text"] == text
    assert body["reply_audio_base64"] is None and body["audio_withheld"] == "PII_EGRESS_BLOCKED"
    assert [r.url.path for r in sarvam.requests] == ["/speech-to-text"]
    assert [r["model"] for r in log.list("acme")] == ["saaras:v2"]


def test_turn_without_pii_has_audio_and_no_withheld_flag(make_client, bearer, monkeypatch, log):
    Sarvam(monkeypatch)
    body = make_client(supervisor_agent=Supervisor()).post(
        "/v1/voice/turn", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"])).json()
    assert body["reply_audio_base64"] == "UklGRg==" and body["audio_withheld"] is None


# V5 -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("env, key", [({}, None), ({"SARVAM_API_KEY": ""}, None), ({"SARVAM_API_KEY": "k1"}, "k1")])
def test_key_comes_from_env(env, key):
    agent = app_module.voice_agent_from_env(env)
    assert agent.api_key == key and agent.offline_mock is False
