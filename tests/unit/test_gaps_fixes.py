"""Fixes for the coverage-gap review: voice upstream failures and Decimal compensation (GAPS-IO-1, GAPS-LOGIC-1)."""

import base64
import json
from decimal import Decimal

import httpx
import pytest

from hrms_plugin.agents.voice import VoiceAgent
from hrms_plugin.security.pii import PiiSession
from hrms_plugin.server import app as app_module

AUDIO = base64.b64encode(b"valid_audio_bytes").decode()
ROUTES = [("/v1/voice/transcribe", {"audio_base64": AUDIO}), ("/v1/voice/synthesize", {"text": "hello"}),
          ("/v1/voice/turn", {"audio_base64": AUDIO})]


def _upstream(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr("hrms_plugin.agents.voice.httpx.AsyncClient",
                        lambda *a, **kw: original(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setattr(app_module, "voice_agent", VoiceAgent(api_key="sarvam-key"))


def _timeout(request):
    raise httpx.ReadTimeout("slow", request=request)


def _refused(request):
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize("route, body", ROUTES)
@pytest.mark.parametrize("handler, status, code", [
    (lambda r: httpx.Response(500, json={"e": 1}), 502, "VOICE_UPSTREAM_ERROR"),
    (lambda r: httpx.Response(429, json={"e": 1}), 502, "VOICE_UPSTREAM_ERROR"),
    (lambda r: httpx.Response(200, content=b"<html>not json"), 502, "VOICE_UPSTREAM_ERROR"),
    (_refused, 502, "VOICE_UPSTREAM_ERROR"),
    (_timeout, 504, "VOICE_UPSTREAM_TIMEOUT"),
])
def test_voice_upstream_failure_is_a_gateway_error(make_client, bearer, monkeypatch, route, body, handler, status,
                                                   code):
    _upstream(monkeypatch, handler)
    res = make_client(raise_server_exceptions=False).post(route, json=body, headers=bearer(roles=["staff"]))
    assert res.status_code == status and res.json() == {"error": code}
    assert "sarvam-key" not in res.text


def test_voice_upstream_success_still_works(make_client, bearer, monkeypatch):
    _upstream(monkeypatch, lambda r: httpx.Response(200, json={"transcript": "hi", "language_code": "en-IN"}))
    res = make_client().post("/v1/voice/transcribe", json={"audio_base64": AUDIO}, headers=bearer(roles=["staff"]))
    assert res.status_code == 200 and res.json()["transcript"] == "hi"


@pytest.mark.parametrize("value, prose", [
    (Decimal("45000.00"), "45000.00"), (Decimal("45000"), "45,000"), (Decimal("450000"), "4,50,000"),
    (Decimal("88276.92"), "88276.92"),
])
def test_decimal_compensation_is_tokenised(value, prose):
    s = PiiSession(tokenize_compensation=True)
    s.learn({"salary": value})
    assert prose not in s.redact_text(f"pay is {prose} INR")


def test_nested_decimal_tool_output_never_reaches_the_prompt_in_plaintext():
    output = {"line_items": [{"allowed_amount": Decimal("3000.00")}], "total_approved": {"amount": Decimal("12500")}}
    s = PiiSession(tokenize_compensation=True)
    s.learn(output)
    text = s.redact_text(json.dumps(output, default=str))
    assert "3000.00" not in text and "12500" not in text


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), True])
def test_non_finite_and_bool_amounts_are_ignored(value):
    s = PiiSession(tokenize_compensation=True)
    s.learn({"salary": value})
    assert s.redact_text("pay is NaN Infinity True") == "pay is NaN Infinity True"


def test_decimal_is_not_tokenised_when_policy_is_off():
    s = PiiSession(tokenize_compensation=False)
    s.learn({"salary": Decimal("45000")})
    assert s.redact_text("pay is 45000") == "pay is 45000"
