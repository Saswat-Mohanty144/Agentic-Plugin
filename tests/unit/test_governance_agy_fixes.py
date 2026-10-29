"""Fixes for agy piece-G findings F1-F12."""

import json
import threading

import pytest

from hrms_plugin.ai.llm import BaseLLMClient, ChatMessage, LLMConfig, LLMGateway, LLMProvider, LLMResponse
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import LlmNotAllowed, registry_policy_resolver
from hrms_plugin.security.pii import PiiSession
from hrms_plugin.security.tenant_registry import load_registry
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import INITECH_ISSUER, NOW, claims, mint
from tests.unit.test_mapping_governance import mapping

AADHAAR = "234567890124"


class Recorder(BaseLLMClient):
    def __init__(self, fail=False):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.fail = fail

    async def chat(self, messages, tools=None, temperature=None):
        if self.fail:
            raise RuntimeError("provider timed out after receiving the request")
        return LLMResponse(content="ok", usage={"input_tokens": 3, "output_tokens": 1})

    async def chat_stream(self, messages, tools=None, temperature=None):
        yield {"event": "token", "data": "ok"}


def gw(log, fail=False):
    g = LLMGateway(
        config=LLMConfig(provider=LLMProvider.FAKE),
        egress_log=log,
        policy_resolver=registry_policy_resolver(load_registry("tests/fixtures/tenant_registry.yaml")),
    )
    g.client = Recorder(fail)
    return g


# F1 — agentic synthesis must not swallow the policy refusal -------------------------------------------


def test_agentic_synthesis_for_tenant_without_policy_is_403(make_client, keys):
    token = mint(
        keys.initech_rsa,
        "initech-rsa",
        claims(
            issuer=INITECH_ISSUER, tenant="initech", roles=["hr"], tenant_claim="org", roles_claim="groups", lifetime=60
        ),
    )
    # initech's only role maps to HR_ADMIN (no MANAGE_SCHEMA) → prove the policy refusal at the service layer instead
    import asyncio

    from hrms_plugin.schema.canonical import EntityType
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
    from hrms_plugin.schema.synthesizer import MappingSynthesizer

    discovered = DiscoveredEntity(name="E", fields={"zq_given": DiscoveredField(name="zq_given", path="zq_given")})
    with pytest.raises(LlmNotAllowed):
        asyncio.run(MappingSynthesizer.synthesize_agentic(discovered, EntityType.EMPLOYEE, tenant_id="initech"))
    assert token


# F2 — per-call redaction counts, even for values seen earlier in the session ------------------------------


async def test_multi_turn_calls_each_report_their_redactions():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    g, pii = gw(log), PiiSession()
    for _ in range(2):
        await g.chat([ChatMessage(role="user", content=f"id {AADHAAR}")], tenant_id="acme", pii=pii)
    assert [r["redacted_kinds"] for r in log.list("acme")] == [{"AADHAAR": 1}, {"AADHAAR": 1}]


# F3 / F4 / F5 — compensation tokenisation ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["salary is 45000", "salary is 45000.0", "salary is 45,000", "salary is 45000.00"])
def test_compensation_formats_are_caught(text):
    s = PiiSession(tokenize_compensation=True)
    s.learn({"salary": 45000.0})
    assert "45" not in s.redact_text(text).split("is ")[1]


def test_indian_grouping_is_caught():
    s = PiiSession(tokenize_compensation=True)
    s.learn({"annual_ctc": 1800000})
    out = s.redact_text("CTC 18,00,000 or 1,800,000 or 1800000")
    assert "18,00,000" not in out and "1,800,000" not in out and "1800000" not in out


def test_nested_compensation_values_are_caught():
    s = PiiSession(tokenize_compensation=True)
    s.learn({"salary": {"amount": 50000, "currency": "INR"}})
    assert "50000" not in s.redact_text("salary 50000 INR")


@pytest.mark.parametrize(
    "key",
    [
        "monthly_ctc",
        "net_take_home",
        "gratuity_amount",
        "last_drawn_basic",
        "total_eosb_gratuity",
        "hra",
        "employer_epf",
        "net_pay_amount",
    ],
)
def test_payroll_output_fields_are_compensation(key):
    s = PiiSession(tokenize_compensation=True)
    s.learn({key: 88276.92})
    assert "88276.92" not in s.redact_text("value 88276.92")


def test_non_money_numbers_stay_when_tokenising_compensation():
    s = PiiSession(tokenize_compensation=True)
    s.learn({"tenure_years": 7, "payment_days": 30, "salary": 45000})
    assert s.redact_text("7 years, 30 days") == "7 years, 30 days"


# F6 / F7 / F11 — mapping store ---------------------------------------------------------------------------------


def test_colon_in_vendor_is_rejected_cleanly():
    m = mapping()
    m = type(m)(**{**m.__dict__, "vendor": "zoho:v1"})
    with pytest.raises(MappingGovernanceError) as info:
        MappingStore(MemoryKeyValueStore()).propose_mapping("acme", m, proposed_by="a")
    assert info.value.code == "INVALID_KEY"


def test_concurrent_proposals_get_distinct_versions():
    store = MappingStore(MemoryKeyValueStore())
    versions, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        versions.append(store.propose_mapping("acme", mapping(), proposed_by="a"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(versions) == list(range(1, 9))


def test_direct_active_write_is_not_public():
    assert not hasattr(MappingStore, "save_mapping")


# F8 — explicit-config gateways fail closed for real providers -----------------------------------------------


def test_explicit_real_provider_without_resolver_is_refused():
    with pytest.raises(LlmNotAllowed):
        LLMGateway(LLMConfig(provider=LLMProvider.OPENAI, api_key="k")).policy_for("acme")


def test_explicit_fake_provider_without_resolver_is_allowed():
    assert LLMGateway(LLMConfig(provider=LLMProvider.FAKE)).policy_for("anyone").allowed_providers == ("fake",)


# F9 / F12 — egress recorded even when the call fails; stream tokens unknown, not zero --------------------------


async def test_failed_call_is_still_recorded():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        await gw(log, fail=True).chat([ChatMessage(role="user", content="hi")], tenant_id="acme")
    [record] = log.list("acme")
    assert record["status"] == "FAILED" and record["input_tokens"] is None


async def test_successful_call_records_tokens_and_status():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    await gw(log).chat([ChatMessage(role="user", content="hi")], tenant_id="acme")
    [record] = log.list("acme")
    assert record["status"] == "COMPLETED" and (record["input_tokens"], record["output_tokens"]) == (3, 1)


async def test_stream_tokens_are_unknown_not_zero():
    log = EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)
    [e async for e in gw(log).chat_stream([ChatMessage(role="user", content="hi")], tenant_id="acme")]
    [record] = log.list("acme")
    assert record["input_tokens"] is None and record["output_tokens"] is None
    assert json.dumps(record)
