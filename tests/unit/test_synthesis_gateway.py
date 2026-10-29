"""Token waste #2: agentic schema mapping reuses the app's gateway and keeps every mapping attribute.

Before: the route built a fresh LLMGateway (client, egress store, SQLite connection) per request, and the upgraded
mapping was rebuilt without version_path / collection_path.
"""

import dataclasses
import json

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.ai import llm as llm_module
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from tests.support.scripted_llm import final
from tests.unit.test_synthesizer_llm import DISCOVERED, unmapped


async def test_upgraded_mapping_keeps_every_attribute(scripted):
    target = unmapped()[0]
    rule_mapping, report = MappingSynthesizer.synthesize(DISCOVERED, EntityType.EMPLOYEE)
    rich = dataclasses.replace(rule_mapping, version_path="meta.version", collection_path="data.rows",
                               writeback={"email": "work_mail"})
    gateway, _ = scripted(final(json.dumps({target: {"host_field": "zq_given"}})))
    upgraded, _ = await MappingSynthesizer._apply_llm_matches(DISCOVERED, rich, report, gateway)
    assert any(f.target == target for f in upgraded.fields)
    assert (upgraded.version_path, upgraded.collection_path, upgraded.writeback) == (
        "meta.version", "data.rows", {"email": "work_mail"})
    assert upgraded.id_path == rich.id_path and upgraded.vendor == rich.vendor


def test_route_uses_the_app_gateway_and_builds_none(make_client, bearer, scripted, monkeypatch):
    target = unmapped()[0]
    gateway, client = scripted(final(json.dumps({target: {"host_field": "zq_given", "confidence": 0.9}})))
    app_client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))

    def refuse(*_a, **_kw):
        raise AssertionError("a new LLMGateway was built for this request")

    monkeypatch.setattr(llm_module.LLMGateway, "__init__", refuse)
    body = {"domain": "EMPLOYEE", "discovered_fields": [{"name": "zq_given"}, {"name": "work_mail"}]}
    res = app_client.post("/v1/schema/synthesize/agentic", json=body, headers=bearer(roles=["platform_admin"]))
    assert res.status_code == 200 and len(client.calls) == 1
    assert any(f["canonical_target"] == target for f in res.json()["field_mappings"])


@pytest.mark.parametrize("missing", ["get_orchestrator"])
def test_route_without_a_supervisor_gateway_is_a_clear_error(make_client, bearer, missing):
    class Bare:
        pass

    res = make_client(supervisor_agent=Bare(), raise_server_exceptions=False).post(
        "/v1/schema/synthesize/agentic", json={"domain": "EMPLOYEE", "discovered_fields": [{"name": "zq_given"}]},
        headers=bearer(roles=["platform_admin"]))
    assert res.status_code == 503 and res.json() == {"error": "LLM_NOT_CONFIGURED"}
