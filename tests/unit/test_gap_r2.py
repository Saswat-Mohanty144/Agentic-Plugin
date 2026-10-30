"""Tests for review findings R4, R5, and R8 (TASK r2).

Covers:
- R5: Streaming ReAct loop citation scraping and refusal handling parity with run().
  - Scrapes citations from answer text on no-tool path.
  - Scrapes citations from streamed token text on partial path.
  - Refusal finish_reason suppresses citation scraping and sets refused: True in result event.
  - Parity comparison between /v1/chat and /v1/chat/stream for identical conversations.
- R8: Schema drift inspector handles nested dotted sources and tuples without false positives.
  - Top-level segment of dotted paths and tuple paths counts as mapped.
  - Genuinely new top-level fields are still detected.
  - Dotted id_path parent segment counts as mapped.
  - Missing expected fields check resolves dotted paths.
- R4: Deploy configuration mounts audit signing key, hosts, and payroll rules read-only.
  - Docker Compose dedicated config directory mount with unset-means-unset semantics.
  - Kubernetes Secret and ConfigMap mounts with optional: true and corresponding env vars.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import LLMResponse
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.drift import DriftSeverity, SchemaDriftInterceptor
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, tool_call

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"
ADMIN = AuthContext(user_id="u-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


def _orch(gateway, max_iterations: int = 5) -> AgenticOrchestrator:
    return AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        max_iterations=max_iterations,
    )


async def _collect(gen) -> List[Dict[str, Any]]:
    return [e async for e in gen]


# =================================================================================================
# R5: Streaming Citation Collection and Refusal Handling Parity with run()
# =================================================================================================


@pytest.mark.asyncio
async def test_r5_stream_no_tool_path_scrapes_citations_from_answer(scripted):
    """Positive: No-tool path in run_stream collects citations from final answer text."""
    answer_text = (
        "Under UAE Federal Decree-Law No. 33 of 2021, Article 9, the probation period "
        "cannot exceed six months."
    )
    gateway, _ = scripted(final(answer_text))
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("What is the probation limit?", jurisdiction="AE", auth=ADMIN))

    result_event = next(e for e in events if e.get("event") == "result")
    citations = result_event["data"]["statutory_citations"]
    assert len(citations) >= 1
    assert any("Article 9" in c for c in citations)
    assert result_event["data"]["partial"] is False
    assert result_event["data"]["truncated"] is False
    assert "refused" not in result_event["data"] or result_event["data"]["refused"] is False


@pytest.mark.asyncio
async def test_r5_stream_partial_path_scrapes_citations_from_streamed_tokens(scripted):
    """Positive: Partial path collects citations from streamed tokens after streaming finishes."""
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        final("As stipulated in Article 15 of Code on Wages 2019, statutory deductions must align."),
    )
    orch = _orch(gateway, max_iterations=1)
    events = await _collect(
        orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="IN", auth=ADMIN)
    )

    result_event = next(e for e in events if e.get("event") == "result")
    result_data = result_event["data"]
    assert result_data["partial"] is True

    citations = result_data["statutory_citations"]
    # Must contain both tool citation and citation scraped from streamed token text
    assert any("Article 15" in c or "Code on Wages" in c for c in citations)


@pytest.mark.asyncio
async def test_r5_stream_no_tool_refusal_no_scraping_and_refused_flag(scripted):
    """Negative/Refusal: Finish reason 'refusal' on no-tool path sets refused: true and scrapes no citations."""
    refusal_text = (
        "I cannot assist with bypassing obligations under UAE Federal Decree-Law No. 33 of 2021, Article 9."
    )
    gateway, _ = scripted(
        LLMResponse(
            content=refusal_text,
            finish_reason="refusal",
            model="scripted",
            provider="scripted",
        )
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("How to evade probation rules?", jurisdiction="AE", auth=ADMIN))

    done_event = next(e for e in events if e.get("event") == "done")
    assert done_event["data"]["finish_reason"] == "refusal"

    result_event = next(e for e in events if e.get("event") == "result")
    result_data = result_event["data"]
    assert result_data.get("refused") is True
    # Citation scraping must NOT happen on refusal
    assert result_data["statutory_citations"] == []


@pytest.mark.asyncio
async def test_r5_stream_partial_refusal_no_scraping_and_refused_flag(scripted):
    """Negative/Refusal: Finish reason 'refusal' on fallback stream sets refused: true and suppresses scraping."""
    refusal_text = "I must decline to summarize under Section 15 of Code on Wages 2019."
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        LLMResponse(
            content=refusal_text,
            finish_reason="refusal",
            model="scripted",
            provider="scripted",
        ),
    )
    orch = _orch(gateway, max_iterations=1)
    events = await _collect(
        orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="IN", auth=ADMIN)
    )

    result_event = next(e for e in events if e.get("event") == "result")
    result_data = result_event["data"]
    assert result_data["partial"] is True
    assert result_data.get("refused") is True
    # Only tool citations may exist from previous rounds, not the refusal text's "Section 15"
    assert not any("Section 15" in c for c in result_data["statutory_citations"])


@pytest.mark.asyncio
async def test_r5_chat_endpoint_vs_stream_endpoint_citations_parity(make_client, bearer, scripted):
    """Parity: Compare /v1/chat citations with /v1/chat/stream result citations for identical scripted response."""
    answer_text = (
        "Pursuant to UAE Federal Decree-Law No. 33 of 2021, Article 9, probation periods "
        "must not exceed 180 days."
    )
    payload = {
        "message": "What is the probation limit under UAE law?",
        "jurisdiction": "AE",
        "agentic": True,
    }
    headers = bearer(tenant="acme", roles=["hr_manager"])

    # 1. Non-streaming call via /v1/chat
    gateway_chat, _ = scripted(final(answer_text))
    client_chat = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway_chat))
    resp_chat = client_chat.post("/v1/chat", json=payload, headers=headers)
    assert resp_chat.status_code == 200
    chat_citations = resp_chat.json()["statutory_citations"]

    # 2. Streaming call via /v1/chat/stream
    gateway_stream, _ = scripted(final(answer_text))
    client_stream = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway_stream))
    resp_stream = client_stream.post("/v1/chat/stream", json=payload, headers=headers)
    assert resp_stream.status_code == 200
    events = [json.loads(line[6:]) for line in resp_stream.text.splitlines() if line.startswith("data: ")]
    result_event = next(e for e in events if e.get("event") == "result")
    stream_citations = result_event["data"]["statutory_citations"]

    # Both must match and not be empty
    assert len(chat_citations) > 0
    assert chat_citations == stream_citations


@pytest.mark.asyncio
async def test_r5_boundary_answer_without_citations_has_empty_citations(scripted):
    """Boundary: Direct answer with no legal codes yields empty statutory citations and no refusal flag."""
    gateway, _ = scripted(final("The team lunch is scheduled for Friday at 1 PM."))
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("When is team lunch?", jurisdiction="IN", auth=ADMIN))

    result_event = next(e for e in events if e.get("event") == "result")
    assert result_event["data"]["statutory_citations"] == []
    assert result_event["data"].get("refused") is not True


# =================================================================================================
# R8: Schema Drift Detection with Nested Sources and Dotted Paths
# =================================================================================================


def test_r8_nested_dotted_source_no_false_positive():
    """Positive: Mapping with dotted source 'profile.contact.email' counts 'profile' as mapped."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="custom_hrms",
        entity_type="EMPLOYEE",
        id_path="id",
        fields=(FieldMap(target="email", source="profile.contact.email"),),
    )
    raw_response = {
        "id": "EMP-100",
        "profile": {"contact": {"email": "user@example.com"}},
    }
    events = interceptor.inspect_response(EntityType.EMPLOYEE, mapping=mapping, raw_response=raw_response)
    # 'profile' is a top-level segment of a mapped path and must NOT be flagged as unmapped drift
    assert events == []


def test_r8_nested_tuple_source_no_false_positive():
    """Positive: Mapping with tuple of dotted sources counts all top-level segments as mapped."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="custom_hrms",
        entity_type="EMPLOYEE",
        id_path="id",
        fields=(
            FieldMap(target="city", source=("work_address.city", "home_address.city")),
        ),
    )
    raw_response = {
        "id": "EMP-100",
        "work_address": {"city": "Dubai"},
    }
    events = interceptor.inspect_response(EntityType.EMPLOYEE, mapping=mapping, raw_response=raw_response)
    assert events == []


def test_r8_genuinely_new_top_level_key_still_reported():
    """Negative: Genuinely new top-level key not in mapping sources is still detected as drift."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="custom_hrms",
        entity_type="EMPLOYEE",
        id_path="id",
        fields=(FieldMap(target="email", source="profile.email"),),
    )
    raw_response = {
        "id": "EMP-100",
        "profile": {"email": "user@example.com"},
        "unmapped_extra_column": "some_value",
    }
    events = interceptor.inspect_response(EntityType.EMPLOYEE, mapping=mapping, raw_response=raw_response)
    assert len(events) == 1
    assert events[0].field_name == "unmapped_extra_column"
    assert events[0].severity == DriftSeverity.INFO


def test_r8_dotted_id_path_counts_top_level_segment_as_mapped():
    """Boundary: Dotted id_path 'meta.employee_id' counts 'meta' as mapped."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="custom_hrms",
        entity_type="EMPLOYEE",
        id_path="meta.employee_id",
        fields=(FieldMap(target="name", source="display_name"),),
    )
    raw_response = {
        "meta": {"employee_id": "EMP-001"},
        "display_name": "Alice Smith",
    }
    events = interceptor.inspect_response(EntityType.EMPLOYEE, mapping=mapping, raw_response=raw_response)
    assert events == []


def test_r8_dict_mapping_nested_paths():
    """Boundary: Dict mapping with dotted paths counts top-level segment as mapped."""
    interceptor = SchemaDriftInterceptor()
    events = interceptor.inspect_response(
        EntityType.EMPLOYEE,
        mapping={"id": "id", "nested.field": "target_field"},
        raw_response={"id": "1", "nested": {"field": "val"}, "unexpected": 42},
    )
    assert len(events) == 1
    assert events[0].field_name == "unexpected"


def test_r8_missing_expected_field_resolves_dotted_paths():
    """Positive: Missing expected field check resolves dotted paths without false alarms."""
    interceptor = SchemaDriftInterceptor()
    raw_response = {
        "id": "EMP-1",
        "nested": {"existing_key": "val"},
    }
    # When expected_fields contains both existing and missing dotted paths
    events = interceptor.inspect_response(
        EntityType.EMPLOYEE,
        mapping={"id": "id"},
        raw_response=raw_response,
        expected_fields=["nested.existing_key", "nested.absent_key"],
    )
    # Only nested.absent_key should be flagged as missing
    field_names = [e.field_name for e in events]
    assert "nested.absent_key" in field_names
    assert "nested.existing_key" not in field_names


# =================================================================================================
# R4: Deploy File Mounts and Path Configuration
# =================================================================================================


def _load_compose() -> Dict[str, Any]:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def _load_k8s_docs() -> List[Dict[str, Any]]:
    path = DEPLOY / "k8s" / "deployment.yaml"
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_r4_compose_dedicated_config_mount_and_pass_through():
    """Positive: Compose declares dedicated config directory mount and documents unset-means-unset."""
    compose = _load_compose()
    text = (DEPLOY / "docker-compose.yml").read_text()
    api = compose["services"]["hrms-plugin-api"]
    volumes = api.get("volumes", [])

    # Dedicated config directory mount ${HRMS_CONFIG_DIR:-./config}:/etc/hrms/config:ro
    config_mount = next((v for v in volumes if "/etc/hrms/config" in str(v)), None)
    assert config_mount is not None, "missing dedicated config directory mount at /etc/hrms/config"
    assert "ro" in str(config_mount), "config directory mount must be read-only (:ro)"
    assert "HRMS_CONFIG_DIR" in str(config_mount), "mount must use HRMS_CONFIG_DIR variable"

    # Explanatory comment in compose file explaining unset means unset
    assert "unset" in text.lower(), "missing comment in docker-compose.yml explaining unset-means-unset semantics"

    # Bare pass-through environment variables preserved
    raw_env = api.get("environment", [])
    for var in ("HRMS_AUDIT_SIGNING_KEY_FILE", "HRMS_HOSTS", "HRMS_PAYROLL_RULES"):
        assert var in raw_env, f"missing pass-through variable {var} in compose environment"


def test_r4_compose_config_validation():
    """Positive: docker compose config -q validates cleanly without errors when config dir is unset."""
    if not shutil.which("docker"):
        pytest.skip("docker not installed")

    cmd = ["docker", "compose", "-f", str(DEPLOY / "docker-compose.yml"), "config", "-q"]
    # HRMS_TENANT_REGISTRY_FILE is required by design; provide a dummy path
    dummy_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "HRMS_TENANT_REGISTRY_FILE": "./deploy/tenants.yaml",
    }
    res = subprocess.run(
        cmd,
        env=dummy_env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert res.returncode == 0, f"docker compose config failed: {res.stderr}"


def test_r4_k8s_mounts_audit_secret_and_configmap():
    """Positive: k8s deployment mounts Secret and ConfigMap read-only and sets env vars."""
    docs = _load_k8s_docs()
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    volumes = pod_spec.get("volumes", [])
    volume_mounts = container.get("volumeMounts", [])

    # 1. Audit key Secret mount
    audit_vol = next((v for v in volumes if v.get("secret", {}).get("secretName") == "hrms-plugin-audit-key"), None)
    assert audit_vol is not None, "missing Secret hrms-plugin-audit-key in pod volumes"
    assert audit_vol.get("secret", {}).get("optional") is True, "audit-key Secret must be optional: true"

    audit_mount = next((m for m in volume_mounts if m.get("mountPath") == "/etc/hrms/audit"), None)
    assert audit_mount is not None, "missing volumeMount at /etc/hrms/audit"
    assert audit_mount.get("readOnly") is True, "audit mount must be readOnly: true"
    assert audit_mount["name"] == audit_vol["name"]

    # 2. ConfigMap mount
    config_vol = next((v for v in volumes if v.get("configMap", {}).get("name") == "hrms-plugin-config"), None)
    assert config_vol is not None, "missing ConfigMap hrms-plugin-config in pod volumes"
    assert config_vol.get("configMap", {}).get("optional") is True, "hrms-plugin-config ConfigMap must be optional"

    config_mount = next((m for m in volume_mounts if m.get("mountPath") == "/etc/hrms/config"), None)
    assert config_mount is not None, "missing volumeMount at /etc/hrms/config"
    assert config_mount.get("readOnly") is True, "config mount must be readOnly: true"
    assert config_mount["name"] == config_vol["name"]

    # 3. Environment variables pointing to mounted files
    env_vars = {item["name"]: item.get("value") for item in container.get("env", []) if "name" in item}
    assert env_vars.get("HRMS_AUDIT_SIGNING_KEY_FILE") == "/etc/hrms/audit/audit_ed25519.pem"
    assert env_vars.get("HRMS_HOSTS") == "/etc/hrms/config/hosts.yaml"
    assert env_vars.get("HRMS_PAYROLL_RULES") == "/etc/hrms/config/payroll_rules.yaml"


def test_r4_deploy_yaml_syntax_valid():
    """Boundary: Ensure all deployment YAML documents parse without errors."""
    compose_path = DEPLOY / "docker-compose.yml"
    k8s_path = DEPLOY / "k8s" / "deployment.yaml"

    yaml.safe_load(compose_path.read_text())
    list(yaml.safe_load_all(k8s_path.read_text()))
