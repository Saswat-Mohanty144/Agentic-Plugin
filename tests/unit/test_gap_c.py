"""Tests for Task C (Decision D3): Field classification integration with Agentic Orchestrator.

Covers:
- MappingStore.active_classification read method.
- Entity normalization (upper-cased, spaces and hyphens -> "_").
- Context keys classified llm_visible: true have values shown in system prompt (same format as entity/record_id/route).
- Keys classified PII have values learned by PII session (state.pii) and redacted from outgoing messages.
- Keys classified COMPENSATION are learned only when tokenize_compensation is true.
- Unclassified keys and llm_visible: false keys behave as names only.
- Server-owned keys (jurisdiction, tenant_id) are never shown.
- Both run() and run_stream() apply these rules.
- Wiring with fake resolver and real MappingStore + HostRegistry.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from hrms_plugin.agents import orchestrator as orch_module
from hrms_plugin.agents.orchestrator import LLM_VISIBLE_CONTEXT_KEYS, AgenticOrchestrator
from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.scripted_llm import final, scripted_gateway

AUTH_ACME = AuthContext(user_id="user_c", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
TOKEN_RE = re.compile(r"\[PII_[A-Z_]+_\d+_[0-9a-f]{8}\]")


def _normalise_entity(raw: Any) -> str:
    func = getattr(orch_module, "normalise_entity", None)
    if func is None:
        raise AttributeError("orchestrator module has no normalise_entity function")
    return func(raw)


def _sample_mapping(vendor: str = "frappe", entity_type: str = "EMPLOYEE") -> EntityMapping:
    return EntityMapping(
        vendor=vendor,
        entity_type=entity_type,
        id_path="name",
        fields=(
            FieldMap(target="employee_id", source="name"),
            FieldMap(target="department", source="department"),
            FieldMap(target="badge_code", source="badge_code"),
            FieldMap(target="annual_bonus", source="bonus"),
        ),
    )


def _setup_active_mapping(
    store: MappingStore,
    tenant_id: str = "acme",
    vendor: str = "frappe",
    entity_type: str = "EMPLOYEE",
    classification: Optional[Dict[str, Any]] = None,
) -> int:
    m = _sample_mapping(vendor=vendor, entity_type=entity_type)
    v = store.propose_mapping(tenant_id, m, proposed_by="alice")
    cls_dict = classification or {
        "department": {"data_class": "INTERNAL", "llm_visible": True},
        "badge_code": {"data_class": "PII", "llm_visible": False},
        "annual_bonus": {"data_class": "COMPENSATION", "llm_visible": False},
        "employee_id": {"data_class": "PII", "llm_visible": False},
    }
    store.approve_version(tenant_id, vendor, entity_type, v, approver="bob", classification=cls_dict)
    return v


# 1. MappingStore.active_classification -------------------------------------------------------------


def test_active_classification_returns_none_when_no_versions():
    store = MappingStore(backend=MemoryKeyValueStore())
    assert store.active_classification("acme", "frappe", "EMPLOYEE") is None


def test_active_classification_returns_none_for_proposed_only():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = _sample_mapping()
    store.propose_mapping("acme", m, proposed_by="alice")
    assert store.active_classification("acme", "frappe", "EMPLOYEE") is None


def test_active_classification_returns_dict_for_active_version():
    store = MappingStore(backend=MemoryKeyValueStore())
    expected = {
        "department": {"data_class": "INTERNAL", "llm_visible": True},
        "badge_code": {"data_class": "PII", "llm_visible": False},
        "annual_bonus": {"data_class": "COMPENSATION", "llm_visible": False},
        "employee_id": {"data_class": "PII", "llm_visible": False},
    }
    _setup_active_mapping(store, classification=expected)
    result = store.active_classification("acme", "frappe", "EMPLOYEE")
    assert result == expected


def test_active_classification_returns_none_when_vendor_or_tenant_mismatch():
    store = MappingStore(backend=MemoryKeyValueStore())
    _setup_active_mapping(store, tenant_id="acme", vendor="frappe", entity_type="EMPLOYEE")
    assert store.active_classification("other_tenant", "frappe", "EMPLOYEE") is None
    assert store.active_classification("acme", "iceipts", "EMPLOYEE") is None
    assert store.active_classification("acme", "frappe", "LEAVE_REQUEST") is None


def test_active_classification_supports_entity_type_enum():
    store = MappingStore(backend=MemoryKeyValueStore())
    _setup_active_mapping(store, tenant_id="acme", vendor="frappe", entity_type="EMPLOYEE")
    result = store.active_classification("acme", "frappe", EntityType.EMPLOYEE)
    assert result is not None
    assert "department" in result


def test_active_classification_returns_latest_active_version():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = _sample_mapping()
    v1 = store.propose_mapping("acme", m, "alice")
    store.approve_version(
        "acme", "frappe", "EMPLOYEE", v1, "bob",
        classification={"department": {"data_class": "INTERNAL", "llm_visible": False}},
    )
    v2 = store.propose_mapping("acme", m, "charlie")
    store.approve_version(
        "acme", "frappe", "EMPLOYEE", v2, "dana",
        classification={"department": {"data_class": "INTERNAL", "llm_visible": True}},
    )
    res = store.active_classification("acme", "frappe", "EMPLOYEE")
    assert res is not None
    assert res["department"]["llm_visible"] is True


# 2. Entity Normalization ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("employee", "EMPLOYEE"),
        ("EMPLOYEE", "EMPLOYEE"),
        ("employee-record", "EMPLOYEE_RECORD"),
        ("employee record", "EMPLOYEE_RECORD"),
        ("  Leave - Request  ", "LEAVE_REQUEST"),
        ("expense_claim", "EXPENSE_CLAIM"),
        ("candidate-profile-v2", "CANDIDATE_PROFILE_V2"),
        (EntityType.EMPLOYEE, "EMPLOYEE"),
    ],
)
def test_normalise_entity_cases(raw, expected):
    assert _normalise_entity(raw) == expected


@pytest.mark.parametrize("empty_val", [None, "", "   ", "---", "  -  "])
def test_normalise_entity_empty_or_boundary(empty_val):
    assert _normalise_entity(empty_val) == ""


# 3. LLM-Visible Context Keys in System Prompt -----------------------------------------------------


@pytest.mark.asyncio
async def test_llm_visible_keys_shown_in_system_prompt():
    gateway, client = scripted_gateway([final("Done.")])
    classification = {
        "department": {"data_class": "INTERNAL", "llm_visible": True},
        "badge_code": {"data_class": "PII", "llm_visible": False},
        "unclassified_param": {"data_class": "INTERNAL", "llm_visible": False},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee",
        "record_id": "EMP-42",
        "route": "/employees/42",
        "department": "Engineering",
        "badge_code": "SECRET-BADGE-007",
        "extra_info": "Some confidential note",
    }
    await orch.run("Hello", jurisdiction="IN", auth=AUTH_ACME, context=ctx)

    system_msgs = [m.content for m in client.calls[0]["messages"] if m.role == "system"]
    prompt = "\n".join(system_msgs)

    # Base set still present
    assert "entity=employee" in prompt
    assert "record_id=EMP-42" in prompt
    assert "route=/employees/42" in prompt
    # Classified llm_visible true is shown
    assert "department=Engineering" in prompt

    # Non-visible and unclassified values are NOT shown
    assert "SECRET-BADGE-007" not in prompt
    assert "Some confidential note" not in prompt

    # Key names are still listed in context fields list
    assert "department" in prompt
    assert "badge_code" in prompt
    assert "extra_info" in prompt


@pytest.mark.asyncio
async def test_base_llm_visible_context_keys_constant():
    assert LLM_VISIBLE_CONTEXT_KEYS == ("entity", "record_id", "route")


@pytest.mark.asyncio
async def test_server_owned_keys_never_shown():
    gateway, client = scripted_gateway([final("Done.")])
    classification = {
        "tenant_id": {"data_class": "PUBLIC", "llm_visible": True},
        "jurisdiction": {"data_class": "PUBLIC", "llm_visible": True},
        "department": {"data_class": "INTERNAL", "llm_visible": True},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee",
        "tenant_id": "malicious_tenant",
        "jurisdiction": "US",
        "department": "Operations",
    }
    await orch.run("Hello", jurisdiction="IN", auth=AUTH_ACME, context=ctx)
    prompt = "\n".join(m.content for m in client.calls[0]["messages"] if m.role == "system")

    # System message sets Tenant ID: acme and Jurisdiction: IN from auth/request, never from context
    assert "tenant_id=malicious_tenant" not in prompt
    assert "jurisdiction=US" not in prompt
    assert "department=Operations" in prompt


# 4. PII Session Learning for Classified PII -------------------------------------------------------


@pytest.mark.asyncio
async def test_classified_pii_learned_even_with_unknown_field_name():
    gateway, client = scripted_gateway([final("Acknowledged.")])
    classification = {
        "custom_secret_key": {"data_class": "PII", "llm_visible": False},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee",
        "custom_secret_key": "CLASSIFIED-ALPHA-99",
    }
    # User message contains the exact classified value
    await orch.run(
        "Look up status for CLASSIFIED-ALPHA-99 please",
        jurisdiction="IN",
        auth=AUTH_ACME,
        context=ctx,
    )

    # Inspect all messages sent to LLM client
    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    assert "CLASSIFIED-ALPHA-99" not in sent_text
    assert TOKEN_RE.search(sent_text) is not None


@pytest.mark.asyncio
async def test_unclassified_custom_field_not_learned_as_pii():
    gateway, client = scripted_gateway([final("Acknowledged.")])
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: None,  # no classification
    )
    ctx = {
        "entity": "employee",
        "custom_project_code": "PROJ-OMEGA-88",
    }
    await orch.run(
        "Look up status for PROJ-OMEGA-88 please",
        jurisdiction="IN",
        auth=AUTH_ACME,
        context=ctx,
    )
    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    # Not a standard PII name, not classified PII -> not redacted
    assert "PROJ-OMEGA-88" in sent_text


# 5. Compensation Learning Policy -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compensation_learned_when_tokenize_compensation_true():
    gateway, client = scripted_gateway([final("Salary noted.")])
    # Configure tenant policy with tokenize_compensation=True
    gateway.policy_resolver = lambda tenant_id, provider: LlmPolicy(
        allowed_providers=("fake",), max_data_tier=2, tokenize_compensation=True
    )
    classification = {
        "custom_stipend": {"data_class": "COMPENSATION", "llm_visible": False},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee",
        "custom_stipend": "75000",
    }
    await orch.run(
        "The stipend is 75000 per month",
        jurisdiction="IN",
        auth=AUTH_ACME,
        context=ctx,
    )
    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    assert "75000" not in sent_text
    assert "[PII_COMPENSATION_" in sent_text


@pytest.mark.asyncio
async def test_compensation_not_learned_when_tokenize_compensation_false():
    gateway, client = scripted_gateway([final("Salary noted.")])
    gateway.policy_resolver = lambda tenant_id, provider: LlmPolicy(
        allowed_providers=("fake",), max_data_tier=2, tokenize_compensation=False
    )
    classification = {
        "custom_stipend": {"data_class": "COMPENSATION", "llm_visible": False},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee",
        "custom_stipend": "75000",
    }
    await orch.run(
        "The stipend is 75000 per month",
        jurisdiction="IN",
        auth=AUTH_ACME,
        context=ctx,
    )
    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    assert "75000" in sent_text


# 6. Stream Execution (run_stream) ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stream_applies_classification_and_pii_rules():
    gateway, client = scripted_gateway([final("Streamed reply.")])
    classification = {
        "department": {"data_class": "INTERNAL", "llm_visible": True},
        "badge_code": {"data_class": "PII", "llm_visible": False},
    }
    orch = AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        classification_for=lambda tenant, entity: classification,
    )
    ctx = {
        "entity": "employee-record",
        "record_id": "REC-1",
        "department": "Logistics",
        "badge_code": "SECRET-BADGE-XYZ",
    }
    events = [
        e async for e in orch.run_stream(
            "Check SECRET-BADGE-XYZ",
            jurisdiction="IN",
            auth=AUTH_ACME,
            context=ctx,
        )
    ]
    assert any(e.get("event") == "token" for e in events)

    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    # Visible field shown
    assert "department=Logistics" in sent_text
    # Classified PII redacted
    assert "SECRET-BADGE-XYZ" not in sent_text
    assert TOKEN_RE.search(sent_text) is not None


# 7. Wiring and End-to-End with Real MappingStore and HostRegistry ----------------------------------


def test_default_orchestrator_resolver_is_none():
    orch = AgenticOrchestrator()
    assert orch.classification_for is None


@pytest.mark.asyncio
async def test_wiring_with_real_mapping_store_and_host_registry(tmp_path, monkeypatch):
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        """
tenants:
  - tenant_id: acme
    vendor: frappe
    base_url: https://frappe.acme.test
    api_key_env: FRAPPE_API_KEY
    api_secret_env: FRAPPE_API_SECRET
    timeout_seconds: 10
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("FRAPPE_API_KEY", "dummy_key")
    monkeypatch.setenv("FRAPPE_API_SECRET", "dummy_secret")

    store = MappingStore(backend=MemoryKeyValueStore())
    _setup_active_mapping(
        store,
        tenant_id="acme",
        vendor="frappe",
        entity_type="EMPLOYEE",
        classification={
            "department": {"data_class": "INTERNAL", "llm_visible": True},
            "badge_code": {"data_class": "PII", "llm_visible": False},
            "annual_bonus": {"data_class": "COMPENSATION", "llm_visible": False},
            "employee_id": {"data_class": "PII", "llm_visible": False},
        },
    )

    resolver = app_module.make_classification_resolver(store)
    # Test resolver directly
    cls_found = resolver("acme", "EMPLOYEE")
    assert cls_found is not None
    assert cls_found["department"]["llm_visible"] is True

    # Test via SupervisorAgent and Orchestrator
    gateway, client = scripted_gateway([final("Supervisor done.")])
    supervisor = SupervisorAgent(llm_gateway=gateway, classification_for=resolver)
    ctx = {
        "entity": "Employee",  # normalisation should handle casing
        "record_id": "EMP-99",
        "department": "Security",
        "badge_code": "TOP-SECRET-001",
    }
    await supervisor.process_agentic(
        "Verify TOP-SECRET-001",
        jurisdiction="IN",
        auth=AUTH_ACME,
        context=ctx,
    )

    sent_text = "\n".join(m.content for m in client.calls[0]["messages"])
    assert "department=Security" in sent_text
    assert "TOP-SECRET-001" not in sent_text
    assert TOKEN_RE.search(sent_text) is not None


# 8. Follow-up Review Findings Tests ----------------------------------------------------------------


def test_orchestrator_rejects_classification_resolver_alias():
    with pytest.raises(TypeError, match="unexpected keyword argument 'classification_resolver'"):
        AgenticOrchestrator(classification_resolver=lambda t, e: None)


def test_supervisor_rejects_classification_resolver_alias():
    with pytest.raises(TypeError, match="unexpected keyword argument 'classification_resolver'"):
        SupervisorAgent(classification_resolver=lambda t, e: None)


def test_orchestrator_and_supervisor_accept_classification_for():
    def resolver(t, e):
        return {"field": {"data_class": "PII"}}

    orch = AgenticOrchestrator(classification_for=resolver)
    assert orch.classification_for is resolver
    sup = SupervisorAgent(classification_for=resolver)
    assert sup.classification_for is resolver
    assert sup.get_orchestrator().classification_for is resolver


def test_orchestrator_does_not_touch_pii_private_members():
    code = Path("hrms_plugin/agents/orchestrator.py").read_text(encoding="utf-8")
    assert "._known" not in code, "orchestrator.py must not access PiiSession._known"
    assert "._learn_money" not in code, "orchestrator.py must not access PiiSession._learn_money"


def test_make_classification_resolver_missing_hrms_hosts_returns_none_quietly(monkeypatch, caplog):
    monkeypatch.delenv("HRMS_HOSTS", raising=False)
    store = MappingStore(backend=MemoryKeyValueStore())
    resolver = app_module.make_classification_resolver(store)
    with caplog.at_level(logging.WARNING):
        res = resolver("acme", "EMPLOYEE")
    assert res is None
    assert len(caplog.records) == 0


def test_make_classification_resolver_empty_hrms_hosts_returns_none_quietly(monkeypatch, caplog):
    monkeypatch.setenv("HRMS_HOSTS", "")
    store = MappingStore(backend=MemoryKeyValueStore())
    resolver = app_module.make_classification_resolver(store)
    with caplog.at_level(logging.WARNING):
        res = resolver("acme", "EMPLOYEE")
    assert res is None
    assert len(caplog.records) == 0


def test_make_classification_resolver_tenant_without_host_returns_none_quietly(tmp_path, monkeypatch, caplog):
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        """
tenants:
  - tenant_id: acme
    vendor: frappe
    base_url: https://frappe.acme.test
    api_key_env: FRAPPE_API_KEY
    api_secret_env: FRAPPE_API_SECRET
    timeout_seconds: 10
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("FRAPPE_API_KEY", "dummy_key")
    monkeypatch.setenv("FRAPPE_API_SECRET", "dummy_secret")
    store = MappingStore(backend=MemoryKeyValueStore())
    resolver = app_module.make_classification_resolver(store)
    with caplog.at_level(logging.WARNING):
        res = resolver("unknown_tenant", "EMPLOYEE")
    assert res is None
    assert len(caplog.records) == 0


def test_make_classification_resolver_invalid_registry_logs_warning_no_values(tmp_path, monkeypatch, caplog):
    bad_yaml = tmp_path / "bad_hosts.yaml"
    bad_yaml.write_text("tenants: [invalid: yaml: {", encoding="utf-8")
    monkeypatch.setenv("HRMS_HOSTS", str(bad_yaml))
    store = MappingStore(backend=MemoryKeyValueStore())
    resolver = app_module.make_classification_resolver(store)
    with caplog.at_level(logging.WARNING):
        res = resolver("secret_tenant", "SECRET_ENTITY")
    assert res is None
    assert len(caplog.records) >= 1
    for record in caplog.records:
        assert record.levelno == logging.WARNING
        assert "secret_tenant" not in record.message
        assert "SECRET_ENTITY" not in record.message


def test_make_classification_resolver_store_failure_logs_warning_no_values(tmp_path, monkeypatch, caplog):
    hosts_yaml = tmp_path / "hosts.yaml"
    hosts_yaml.write_text(
        """
tenants:
  - tenant_id: acme
    vendor: frappe
    base_url: https://frappe.acme.test
    api_key_env: FRAPPE_API_KEY
    api_secret_env: FRAPPE_API_SECRET
    timeout_seconds: 10
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_yaml))
    monkeypatch.setenv("FRAPPE_API_KEY", "dummy_key")
    monkeypatch.setenv("FRAPPE_API_SECRET", "dummy_secret")
    store = MagicMock(spec=MappingStore)
    store.active_classification.side_effect = RuntimeError("database connection failed")
    resolver = app_module.make_classification_resolver(store)
    with caplog.at_level(logging.WARNING):
        res = resolver("acme", "EMPLOYEE")
    assert res is None
    assert len(caplog.records) >= 1
    for record in caplog.records:
        assert record.levelno == logging.WARNING
        assert "acme" not in record.message
        assert "EMPLOYEE" not in record.message
