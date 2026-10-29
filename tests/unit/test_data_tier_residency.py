"""QF (Q7): model use by data-sensitivity tier, data residency, and egress-log retention / deletion on request.

Tier 1 = schema metadata only (mapping extraction); tier 2 = pseudonymised operational data (chat, agents). Raw PII
(tier 3) never leaves: every outbound payload is redacted. Tenants default to tier 1 (fail closed); tier, region and
retention are owner inputs per tenant.
"""

import asyncio

import pytest
from pydantic import ValidationError

from hrms_plugin.ai.llm import ChatMessage, LLMConfig, LLMGateway, LLMProvider, TaskClass
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import LlmNotAllowed
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.memory_store import MemoryKeyValueStore

MSG = [ChatMessage(role="user", content="hello")]


def gateway(policy, region=None):
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE), policy_resolver=lambda tenant, provider: policy)
    gw.provider_region = region
    return gw


def chat(gw, task_class):
    return asyncio.run(gw.chat(messages=MSG, task_class=task_class, tenant_id="acme"))


# policy ------------------------------------------------------------------------------------------------------
def test_defaults_fail_closed():
    p = LlmPolicy(allowed_providers=("fake",))
    assert p.max_data_tier == 1 and p.region is None and p.egress_retention_days is None


@pytest.mark.parametrize("field, value", [("max_data_tier", 3), ("max_data_tier", 0), ("max_data_tier", True),
                                          ("region", "Mumbai!"), ("region", ""), ("egress_retention_days", 0)])
def test_invalid_values_rejected(field, value):
    with pytest.raises(ValidationError):
        LlmPolicy(allowed_providers=("fake",), **{field: value})


# tier --------------------------------------------------------------------------------------------------------
def test_tier1_tenant_may_run_schema_extraction_only():
    gw = gateway(LlmPolicy(allowed_providers=("fake",)))
    assert chat(gw, TaskClass.EXTRACTION) is not None
    with pytest.raises(LlmNotAllowed) as exc:
        chat(gw, TaskClass.AGENT_REACT)
    assert exc.value.code == "DATA_TIER_NOT_ALLOWED"


@pytest.mark.parametrize("task", [TaskClass.AGENT_REACT, TaskClass.POLICY_QA, TaskClass.EXTRACTION])
def test_tier2_tenant_may_run_everything(task):
    assert chat(gateway(LlmPolicy(allowed_providers=("fake",), max_data_tier=2)), task) is not None


def test_tier_refusal_happens_before_anything_is_sent():
    log = EgressLog(MemoryKeyValueStore())
    gw = gateway(LlmPolicy(allowed_providers=("fake",)))
    gw.egress_log = log
    with pytest.raises(LlmNotAllowed):
        chat(gw, TaskClass.AGENT_REACT)
    assert log.list("acme") == []


# residency ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("required, actual, allowed", [
    (None, None, True), (None, "eu-west", True), ("ap-south", "ap-south", True),
    ("ap-south", "us-east", False), ("ap-south", None, False),
])
def test_region_must_match_when_required(required, actual, allowed):
    gw = gateway(LlmPolicy(allowed_providers=("fake",), max_data_tier=2, region=required), region=actual)
    if allowed:
        assert chat(gw, TaskClass.AGENT_REACT) is not None
    else:
        with pytest.raises(LlmNotAllowed) as exc:
            chat(gw, TaskClass.AGENT_REACT)
        assert exc.value.code == "DATA_RESIDENCY_MISMATCH"


def test_env_gateway_reads_the_provider_region(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER_REGION", "ap-south")
    monkeypatch.setenv("HRMS_DATA_DIR", str(tmp_path))
    assert LLMGateway().provider_region == "ap-south"


def test_http_tier_refusal_is_403_with_code(make_client, bearer, monkeypatch):
    def refuse(*_a, **_kw):
        raise LlmNotAllowed("tier", code="DATA_TIER_NOT_ALLOWED")

    client = make_client(raise_server_exceptions=False)
    monkeypatch.setattr(client.app.state.supervisor, "process_agentic", refuse)
    res = client.post("/v1/chat", json={"message": "hi", "jurisdiction": "IN", "agentic": True},
                      headers=bearer(roles=["staff"]))
    assert res.status_code == 403 and res.json() == {"error": "DATA_TIER_NOT_ALLOWED"}


# retention ---------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 10 * 86400.0

    def __call__(self):
        return self.now


def _log_with_rows():
    clock = Clock()
    log = EgressLog(MemoryKeyValueStore(), clock=clock)
    for tenant in ("acme", "globex"):
        log.record(tenant_id=tenant, provider="fake", model="m", payload_sha256="sha256:x", redacted_kinds={})
    clock.now += 5 * 86400
    log.record(tenant_id="acme", provider="fake", model="m", payload_sha256="sha256:y", redacted_kinds={})
    return log, clock


@pytest.mark.parametrize("days, left", [(6, 2), (5, 2), (4, 1)])
def test_purge_keeps_rows_within_retention(days, left):
    log, _ = _log_with_rows()
    log.purge("acme", retention_days=days)
    assert len(log.list("acme")) == left and len(log.list("globex")) == 1


def test_delete_tenant_removes_only_that_tenant():
    log, _ = _log_with_rows()
    assert log.delete_tenant("acme") == 2
    assert log.list("acme") == [] and len(log.list("globex")) == 1


def test_http_delete_requires_governance_permission_and_fresh_token(make_client, bearer, monkeypatch):
    log, _ = _log_with_rows()
    monkeypatch.setattr(app_module, "egress_log", log)
    client = make_client()
    assert client.delete("/v1/governance/llm-egress", headers=bearer(roles=["hr_manager"])).status_code == 403
    res = client.delete("/v1/governance/llm-egress", headers=bearer(roles=["platform_admin"]))
    assert res.status_code == 200 and res.json() == {"tenant_id": "acme", "deleted": 2}
    assert log.list("acme") == [] and len(log.list("globex")) == 1
