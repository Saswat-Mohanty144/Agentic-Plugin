"""LLM egress audit log, tenant LLM policy, compensation tokenisation (piece G: G1-G3)."""

import json

import pytest

from hrms_plugin.ai.llm import BaseLLMClient, ChatMessage, LLMConfig, LLMGateway, LLMProvider, LLMResponse
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import LlmNotAllowed, registry_policy_resolver
from hrms_plugin.security.pii import PiiSession
from hrms_plugin.security.tenant_registry import load_registry
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import NOW

AADHAAR = "234567890124"
REGISTRY = "tests/fixtures/tenant_registry.yaml"


class Recorder(BaseLLMClient):
    def __init__(self):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.seen = []

    async def chat(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        return LLMResponse(content="ok", usage={"input_tokens": 11, "output_tokens": 2})

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        yield {"event": "token", "data": "ok"}
        yield {"event": "done", "data": {}}


def gateway(provider="fake", log=None):
    gw = LLMGateway(
        config=LLMConfig(provider=LLMProvider(provider)),
        egress_log=log,
        policy_resolver=registry_policy_resolver(load_registry(REGISTRY)),
    )
    gw.client = Recorder()
    return gw


# G1 ---------------------------------------------------------------------------------------------------


async def test_each_call_is_logged_without_content():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    gw = gateway(log=log)
    await gw.chat([ChatMessage(role="user", content=f"Aadhaar {AADHAAR} for review")], tenant_id="acme")
    [record] = log.list("acme")
    assert record["provider"] == "fake" and record["redacted_kinds"] == {"AADHAAR": 1}
    assert record["input_tokens"] == 11 and record["output_tokens"] == 2
    assert record["payload_sha256"].startswith("sha256:") and len(record["payload_sha256"]) == 71
    assert AADHAAR not in json.dumps(record) and "review" not in json.dumps(record)


async def test_stream_calls_are_logged_too():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    [e async for e in gateway(log=log).chat_stream([ChatMessage(role="user", content="hi")], tenant_id="acme")]
    assert len(log.list("acme")) == 1


async def test_listing_is_tenant_scoped_newest_first_and_capped():
    clock = {"t": 0.0}
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: clock["t"])
    for i in range(201):
        clock["t"] = float(i)
        log.record(
            tenant_id="acme",
            provider="fake",
            model="m",
            payload_sha256="sha256:" + "0" * 64,
            redacted_kinds={},
            input_tokens=0,
            output_tokens=0,
        )
    log.record(
        tenant_id="globex",
        provider="fake",
        model="m",
        payload_sha256="sha256:" + "0" * 64,
        redacted_kinds={},
        input_tokens=0,
        output_tokens=0,
    )
    rows = log.list("acme")
    assert len(rows) == 200 and rows[0]["at"] == 200.0 and rows[-1]["at"] == 1.0
    assert all(r["tenant_id"] == "acme" for r in rows)


def test_egress_endpoint_requires_view_compliance(make_client, bearer):
    client = make_client()
    assert client.get("/v1/governance/llm-egress", headers=bearer(roles=["staff"])).status_code == 403
    res = client.get("/v1/governance/llm-egress", headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 200 and res.json()["tenant_id"] == "acme"


# G2 ---------------------------------------------------------------------------------------------------


async def test_allowed_provider_proceeds():
    gw = gateway()
    assert (await gw.chat([ChatMessage(role="user", content="hi")], tenant_id="acme")).content == "ok"


@pytest.mark.parametrize("tenant,provider", [("initech", "fake"), ("acme", "openai"), ("nobody", "fake")])
async def test_refused_before_anything_is_sent(tenant, provider):
    gw = gateway(provider=provider)
    with pytest.raises(LlmNotAllowed):
        await gw.chat([ChatMessage(role="user", content="hi")], tenant_id=tenant)
    assert gw.client.seen == []


def test_empty_allowed_providers_rejected_at_load(tmp_path):
    import yaml

    from hrms_plugin.security.tenant_registry import RegistryError

    data = yaml.safe_load(open(REGISTRY))
    data["tenants"][0]["llm_policy"]["allowed_providers"] = []
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(RegistryError):
        load_registry(path)


def test_env_built_gateway_enforces_policy(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    gw = LLMGateway()
    assert gw.policy_for("acme").tokenize_compensation is False
    with pytest.raises(LlmNotAllowed):
        gw.policy_for("initech")


def test_http_tenant_without_policy_gets_403(make_client, keys):
    from hrms_plugin.agents.supervisor import SupervisorAgent
    from tests.support.auth import INITECH_ISSUER, claims, mint

    token = mint(
        keys.initech_rsa,
        "initech-rsa",
        claims(
            issuer=INITECH_ISSUER, tenant="initech", roles=["hr"], tenant_claim="org", roles_claim="groups", lifetime=60
        ),
    )
    client = make_client(supervisor_agent=SupervisorAgent())
    res = client.post(
        "/v1/chat", headers={"Authorization": f"Bearer {token}"}, json={"message": "hi", "jurisdiction": "IN"}
    )
    assert res.status_code == 403 and res.json() == {"error": "LLM_NOT_ALLOWED_FOR_TENANT"}
    stream = client.post(
        "/v1/chat/stream", headers={"Authorization": f"Bearer {token}"}, json={"message": "hi", "jurisdiction": "IN"}
    )
    assert stream.status_code == 403


# G3 ---------------------------------------------------------------------------------------------------


def test_compensation_tokenised_when_enabled():
    s = PiiSession(tokenize_compensation=True)
    s.learn({"employees": [{"id": "E1", "basic_salary": 45000.0, "ctc": "1800000"}]})
    red = s.redact_text("basic 45000.0 and ctc 1800000")
    assert "45000.0" not in red and "1800000" not in red
    assert s.restore(red) == "basic 45000.0 and ctc 1800000"


def test_compensation_kept_when_disabled():
    s = PiiSession()
    s.learn({"basic_salary": 45000.0})
    assert "45000.0" in s.redact_text("basic 45000.0")
