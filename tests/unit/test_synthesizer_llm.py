"""LLM-assisted schema mapping is applied, not discarded; failures are logged (piece E5)."""

import json
import logging

import pytest

from hrms_plugin.ai.llm import LLMNotConfigured
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from tests.support.scripted_llm import final

DISCOVERED = DiscoveredEntity(name="Employee", fields={
    "zq_given": DiscoveredField(name="zq_given", path="zq_given"),
    "work_mail": DiscoveredField(name="work_mail", path="work_mail"),
})


def unmapped():
    _mapping, report = MappingSynthesizer.synthesize(DISCOVERED, EntityType.EMPLOYEE)
    assert "zq_given" in report.unmapped_host_fields, "precondition: rules must leave zq_given unmapped"
    return report.unmapped_canonical_fields


async def test_llm_match_is_applied_to_mapping_and_report(scripted):
    target = unmapped()[0]
    gateway, client = scripted(final(json.dumps({target: {"host_field": "zq_given", "confidence": 0.9,
                                                         "rationale": "given name"}})))
    mapping, report = await MappingSynthesizer.synthesize_agentic(DISCOVERED, EntityType.EMPLOYEE, llm_gateway=gateway)
    assert any(f.target == target and f.source == "zq_given" for f in mapping.fields)
    assert any(s.canonical_target == target and s.confidence == 0.9 for s in report.field_mappings)
    assert target not in report.unmapped_canonical_fields and "zq_given" not in report.unmapped_host_fields
    assert len(client.calls) == 1


async def test_fenced_json_is_accepted(scripted):
    target = unmapped()[0]
    gateway, _ = scripted(final("```json\n" + json.dumps({target: {"host_field": "zq_given"}}) + "\n```"))
    mapping, _ = await MappingSynthesizer.synthesize_agentic(DISCOVERED, EntityType.EMPLOYEE, llm_gateway=gateway)
    assert any(f.target == target for f in mapping.fields)


async def test_invalid_json_is_logged_and_rule_mapping_returned(scripted, caplog):
    caplog.set_level(logging.WARNING)
    rule_mapping, _ = MappingSynthesizer.synthesize(DISCOVERED, EntityType.EMPLOYEE)
    gateway, _ = scripted(final("not json"))
    mapping, _ = await MappingSynthesizer.synthesize_agentic(DISCOVERED, EntityType.EMPLOYEE, llm_gateway=gateway)
    assert mapping.fields == rule_mapping.fields
    assert any(getattr(r, "reason", "") == "LLM_MAPPING_FAILED" for r in caplog.records)


async def test_unknown_host_field_is_ignored(scripted):
    target = unmapped()[0]
    rule_mapping, _ = MappingSynthesizer.synthesize(DISCOVERED, EntityType.EMPLOYEE)
    gateway, _ = scripted(final(json.dumps({target: {"host_field": "does_not_exist"}})))
    mapping, _ = await MappingSynthesizer.synthesize_agentic(DISCOVERED, EntityType.EMPLOYEE, llm_gateway=gateway)
    assert mapping.fields == rule_mapping.fields


async def test_missing_llm_configuration_is_not_swallowed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bard")
    with pytest.raises(LLMNotConfigured):
        await MappingSynthesizer.synthesize_agentic(DISCOVERED, EntityType.EMPLOYEE)
