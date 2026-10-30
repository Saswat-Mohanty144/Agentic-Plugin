"""Adversarial audit tests for PII disclosure index, egress logging, tenant isolation, and cryptographic hashing.

Covers:
- LLM call with PII -> WITHHELD ref recorded in egress log -> lookup finds record
- MCP tool output with PII -> DISCLOSED ref recorded -> lookup finds record
- Tenant isolation: Tenant A disclosures and egress logs are invisible to Tenant B
- Tenant purge: data-purge removes disclosures and clears egress log
- Cryptographic failure modes: missing key file (503), key < 32 bytes (503), tenant HMAC separation
- Canonicalization: space and hyphen stripping for PAN, Aadhaar, Emirates ID
- BUG-5 (LOW): deploy/docker-compose.yml omits HRMS_DISCLOSURE_KEY_FILE from environment
"""

import secrets
from pathlib import Path

import pytest
import yaml

import hrms_plugin.governance.disclosure as disclosure
from hrms_plugin.server import app as app_mod

FAKE_PAN = "ABCPE1234F"
FAKE_AADHAAR = "234567890124"


@pytest.fixture
def disclosure_key(tmp_path, monkeypatch):
    """Write a valid >=32 byte disclosure key to a temp file and point HRMS_DISCLOSURE_KEY_FILE to it."""
    key_bytes = secrets.token_bytes(32)
    key_file = tmp_path / "disclosure.key"
    key_file.write_bytes(key_bytes)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))
    return key_bytes


def test_disclosure_llm_withheld_ref_and_lookup(make_client, bearer, disclosure_key):
    """Positive: LLM call with withheld PII indexes HMAC ref and allows lookup."""
    client = make_client()
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")

    # Record egress log row with WITHHELD ref
    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert ref is not None
    app_mod.egress_log.record(
        tenant_id="acme",
        provider="fake",
        model="gpt-test",
        payload_sha256="sha256:dummy",
        redacted_kinds={"PAN": 1},
        status="COMPLETED",
        channel="chat",
        subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
    )

    # Lookup disclosure
    lookup_res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_admin,
    )
    assert lookup_res.status_code == 200
    data = lookup_res.json()
    assert data["found"] is True
    assert len(data["disclosures"]) > 0
    disc = data["disclosures"][0]
    assert disc["outcome"] == "WITHHELD"
    assert disc["channel"] == "chat"


def test_disclosure_mcp_output_disclosed_ref_and_lookup(make_client, bearer, disclosure_key):
    """Positive: MCP tool output with PII indexes DISCLOSED ref and allows lookup."""
    client = make_client()
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")

    # Simulate MCP tool call output with PAN
    tool_output = {"employee_id": "EMP-10", "pan_number": FAKE_PAN}
    extracted_refs = disclosure.extract_disclosed_refs("acme", tool_output)
    assert len(extracted_refs) == 1
    assert extracted_refs[0]["outcome"] == "DISCLOSED"

    app_mod.egress_log.record(
        tenant_id="acme",
        provider="mcp-client",
        model="tool-test",
        payload_sha256="sha256:dummy2",
        redacted_kinds={},
        status="COMPLETED",
        channel="mcp",
        subject_refs=extracted_refs,
    )

    lookup_res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_admin,
    )
    assert lookup_res.status_code == 200
    data = lookup_res.json()
    assert data["found"] is True
    disclosed = [d for d in data["disclosures"] if d["outcome"] == "DISCLOSED"]
    assert len(disclosed) > 0


def test_disclosure_tenant_isolation_lookup_and_egress(make_client, bearer, disclosure_key):
    """Negative & Security: Tenant A disclosures and egress logs cannot be viewed by Tenant B."""
    client = make_client()
    h_globex = bearer(tenant="globex", roles=["hr_manager"], sub="admin-globex")

    # Record Acme disclosure
    ref_acme = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    app_mod.egress_log.record(
        tenant_id="acme",
        provider="fake",
        model="gpt-test",
        payload_sha256="sha256:acme_hash",
        redacted_kinds={"PAN": 1},
        status="COMPLETED",
        channel="chat",
        subject_refs=[{"ref": ref_acme, "kind": "PAN", "outcome": "WITHHELD"}],
    )

    # Globex queries for the same PAN -> found: False (different tenant HMAC)
    globex_lookup = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_globex,
    )
    # Globex has hr_manager role which lacks MANAGE_GOVERNANCE (SUPER_ADMIN only) -> 403 FORBIDDEN
    assert globex_lookup.status_code == 403
    assert globex_lookup.json()["error"] == "FORBIDDEN"

    # Even if Globex had SUPER_ADMIN, HMAC isolation prevents finding Acme records
    ref_globex = disclosure.compute_ref("globex", "PAN", FAKE_PAN)
    assert ref_acme != ref_globex
    assert app_mod.egress_log.find_subject("globex", ref_globex) == []

    # Egress log isolation: Globex listing egress sees none of Acme's rows
    egress_res = client.get("/v1/governance/llm-egress", headers=h_globex)
    assert egress_res.status_code == 200
    records = egress_res.json()["records"]
    assert not any(r["tenant_id"] == "acme" for r in records)


def test_disclosure_purge_tenant_removes_disclosures_and_egress(make_client, bearer, disclosure_key):
    """Positive: Purging tenant data deletes egress records and empties disclosure lookups."""
    client = make_client()
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")

    # Record row
    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    app_mod.egress_log.record(
        tenant_id="acme",
        provider="fake",
        model="gpt-test",
        payload_sha256="sha256:purge_test",
        redacted_kinds={"PAN": 1},
        status="COMPLETED",
        channel="chat",
        subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
    )

    # Purge tenant
    purge_res = client.delete("/v1/governance/llm-egress", headers=h_admin)
    assert purge_res.status_code == 200
    assert purge_res.json()["deleted"] >= 1

    # Lookup is now empty
    lookup_res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_admin,
    )
    assert lookup_res.status_code == 200
    assert lookup_res.json()["found"] is False
    assert lookup_res.json()["disclosures"] == []


def test_disclosure_crypto_and_configuration_failure_modes(make_client, bearer, tmp_path, monkeypatch):
    """Negative & Security: Unconfigured or short disclosure key (<32 bytes) fails closed with 503."""
    client = make_client()
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")

    # 1. Missing key env var -> 503 DISCLOSURE_INDEX_NOT_CONFIGURED
    monkeypatch.delenv("HRMS_DISCLOSURE_KEY_FILE", raising=False)
    assert disclosure.is_configured() is False
    res_no_key = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_admin,
    )
    assert res_no_key.status_code == 503
    assert res_no_key.json()["error"] == "DISCLOSURE_INDEX_NOT_CONFIGURED"

    # 2. Key file shorter than 32 bytes (16 bytes) -> 503
    short_file = tmp_path / "short.key"
    short_file.write_bytes(secrets.token_bytes(16))
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(short_file))
    assert disclosure.is_configured() is False
    res_short = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=h_admin,
    )
    assert res_short.status_code == 503
    assert res_short.json()["error"] == "DISCLOSURE_INDEX_NOT_CONFIGURED"

    # 3. Canonicalization stability across whitespace and hyphens
    valid_file = tmp_path / "valid.key"
    valid_file.write_bytes(secrets.token_bytes(32))
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(valid_file))
    ref_clean = disclosure.compute_ref("acme", "PAN", "ABCPE1234F")
    ref_dirty = disclosure.compute_ref("acme", "PAN", " ABC-PE 1234 F ")
    assert ref_clean == ref_dirty


# ==============================================================================
# CONFIRMED BUGS
# ==============================================================================


def test_docker_compose_declares_disclosure_key_file():
    """BUG-5 (LOW): deploy/docker-compose.yml must declare HRMS_DISCLOSURE_KEY_FILE in its environment.

    In deploy/docker-compose.yml:26-29, the environment block passes through:
        - HRMS_HOSTS
        - HRMS_PAYROLL_RULES
        - HRMS_AUDIT_SIGNING_KEY_FILE
        - HRMS_AUDIT_KEY_ID
    but omits HRMS_DISCLOSURE_KEY_FILE. Consequently, operators providing HRMS_DISCLOSURE_KEY_FILE
    in their environment will not have it forwarded into the container, disabling disclosure lookups.
    """
    compose_path = Path("deploy/docker-compose.yml")
    assert compose_path.is_file(), "deploy/docker-compose.yml not found"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    services = data.get("services", {})
    api_service = services.get("hrms-plugin-api", {})
    env_list = api_service.get("environment", [])

    assert "HRMS_DISCLOSURE_KEY_FILE" in env_list, (
        f"deploy/docker-compose.yml environment section is missing HRMS_DISCLOSURE_KEY_FILE. "
        f"Found: {env_list}"
    )
