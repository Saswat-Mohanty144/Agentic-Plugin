"""Tests for Task K2: Disclosure index on the egress log and lookup endpoint (K2.1-K2.4)."""

import json
import secrets
from typing import Any, Dict, Optional

import pytest

import hrms_plugin.governance.disclosure as disclosure
from hrms_plugin.ai.llm import BaseLLMClient, ChatMessage, LLMConfig, LLMGateway, LLMProvider, LLMResponse
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import registry_policy_resolver
from hrms_plugin.security.pii import PiiSession
from hrms_plugin.security.tenant_registry import load_registry
from hrms_plugin.server import app as app_module
from hrms_plugin.server.mcp_server import HrmsMcpServer
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import NOW

FAKE_PAN = "ABCPE1234F"
FAKE_AADHAAR = "234567890124"
FAKE_EMAIL = "user@example.test"
FAKE_PHONE = "+91 98765 43210"
REGISTRY = "tests/fixtures/tenant_registry.yaml"


@pytest.fixture
def disclosure_key(tmp_path, monkeypatch):
    """Write a valid >=32 byte disclosure key to a temp file and point HRMS_DISCLOSURE_KEY_FILE to it."""
    key_bytes = secrets.token_bytes(32)
    key_file = tmp_path / "disclosure.key"
    key_file.write_bytes(key_bytes)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))
    return key_bytes


# --- Unit Tests: K2.2 Key, Canonical, and Reference Generation ---------------------------------------


def test_ref_is_stable_for_same_tenant_kind_value(disclosure_key):
    """Ref is deterministic and stable for identical tenant, kind, and canonical value."""
    ref1 = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    ref2 = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert ref1 is not None
    assert ref1.startswith("hmac-sha256:")
    assert ref1 == ref2


def test_ref_differs_across_tenants_and_kinds(disclosure_key):
    """Ref varies when tenant, kind, or value changes."""
    base_ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    other_tenant_ref = disclosure.compute_ref("globex", "PAN", FAKE_PAN)
    other_kind_ref = disclosure.compute_ref("acme", "AADHAAR", FAKE_PAN)
    other_val_ref = disclosure.compute_ref("acme", "PAN", "XYZPE1234F")

    assert base_ref != other_tenant_ref
    assert base_ref != other_kind_ref
    assert base_ref != other_val_ref


def test_canonical_removes_spaces_hyphens_only_for_listed_kinds():
    """canonical: NFKC normalise, casefold, strip; remove spaces/hyphens ONLY for listed kinds."""
    listed = [
        ("AADHAAR", " 2345-6789 0124 ", "234567890124"),
        ("PAN", " ABC-PE 1234 F ", "abcpe1234f"),
        ("CARD", " 4111-2222 3333 4444 ", "4111222233334444"),
        ("IBAN", " GB82-WEST 1234-5698 ", "gb82west12345698"),
        ("SSN", " 123-45-6789 ", "123456789"),
        ("EMIRATES_ID", " 784-1234-1234567-1 ", "784123412345671"),
        ("PHONE", " +91-98765 43210 ", "+919876543210"),
    ]
    for kind, raw, expected in listed:
        assert disclosure.canonical(kind, raw) == expected

    unlisted = [
        ("EMAIL", " Test-User@Example.COM ", "test-user@example.com"),
        ("NAME", " Tariq Al-Mansoor ", "tariq al-mansoor"),
        ("ADDRESS", " 123 Main-St Suite 4 ", "123 main-st suite 4"),
        ("COMPENSATION", " 50,000-INR ", "50,000-inr"),
    ]
    for kind, raw, expected in unlisted:
        assert disclosure.canonical(kind, raw) == expected


def test_key_missing_short_empty_env(tmp_path, monkeypatch):
    """Missing, empty, non-existent, or <32 byte key files mean disclosure is not configured."""
    monkeypatch.delenv("HRMS_DISCLOSURE_KEY_FILE", raising=False)
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False
    assert disclosure.compute_ref("acme", "PAN", FAKE_PAN) is None

    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", "")
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False

    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(tmp_path / "nonexistent.key"))
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False

    short_file = tmp_path / "short.key"
    short_file.write_bytes(b"short_key_under_32_bytes_len")
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(short_file))
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False

    valid_file = tmp_path / "valid.key"
    valid_file.write_bytes(b"x" * 32)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(valid_file))
    assert disclosure.get_disclosure_key() == b"x" * 32
    assert disclosure.is_configured() is True


def test_pii_session_original_value_public_method():
    """PiiSession provides a public original_value(token) returning original value without persistence."""
    session = PiiSession()
    redacted = session.redact_text(f"Audit PAN {FAKE_PAN} now")
    assert FAKE_PAN not in redacted

    import re
    match = re.search(r"\[PII_PAN_\d+_[0-9a-f]{8}\]", redacted)
    assert match is not None
    token = match.group(0)

    assert session.original_value(token) == FAKE_PAN
    assert session.original_value("[PII_PAN_999_deadbeef]") is None


# --- Unit Tests: K2.1 EgressLog Indexing, Purging, and Deletion ---------------------------------------


def test_egress_log_record_and_update_subject_refs(disclosure_key):
    """EgressLog.record and update record channel and subject_refs, maintaining llm_egress_subjects index."""
    store = MemoryKeyValueStore()
    log = EgressLog(store, clock=lambda: NOW)

    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    subject_refs = [{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}]

    key = log.record(
        tenant_id="acme",
        provider="fake",
        model="m1",
        payload_sha256="sha256:" + "0" * 64,
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=subject_refs,
    )

    row = store.get("llm_egress", key)
    assert row is not None
    assert row["channel"] == "llm"
    assert row["subject_refs"] == subject_refs
    assert row["subject_index"] is True

    # Check subject index
    idx = store.get("llm_egress_subjects", f"acme:{ref}")
    assert idx is not None
    assert key in idx.get("keys", [])


def test_egress_log_unconfigured_key_sets_subject_index_false(monkeypatch):
    """When key is missing, subject_refs is [] and subject_index is False."""
    monkeypatch.delenv("HRMS_DISCLOSURE_KEY_FILE", raising=False)
    store = MemoryKeyValueStore()
    log = EgressLog(store, clock=lambda: NOW)

    key = log.record(
        tenant_id="acme",
        provider="fake",
        model="m1",
        payload_sha256="sha256:" + "0" * 64,
        redacted_kinds={},
        channel="llm",
        subject_refs=[{"ref": "fake_ref", "kind": "PAN", "outcome": "WITHHELD"}],
    )

    row = store.get("llm_egress", key)
    assert row is not None
    assert row["subject_refs"] == []
    assert row["subject_index"] is False


def test_purge_and_delete_tenant_remove_index_entries(disclosure_key):
    """Purge and delete_tenant remove index entries from llm_egress_subjects."""
    clock = {"t": 1000.0}
    store = MemoryKeyValueStore()
    log = EgressLog(store, clock=lambda: clock["t"])

    ref_pan = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    ref_aadhaar = disclosure.compute_ref("acme", "AADHAAR", FAKE_AADHAAR)

    # Record row 1 at t=1000
    log.record(
        tenant_id="acme",
        provider="fake",
        model="m1",
        payload_sha256="sha256:" + "1" * 64,
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref_pan, "kind": "PAN", "outcome": "WITHHELD"}],
    )

    # Advance clock and record row 2 at t=200000
    clock["t"] = 200000.0
    log.record(
        tenant_id="acme",
        provider="fake",
        model="m2",
        payload_sha256="sha256:" + "2" * 64,
        redacted_kinds={"AADHAAR": 1},
        channel="llm",
        subject_refs=[{"ref": ref_aadhaar, "kind": "AADHAAR", "outcome": "WITHHELD"}],
    )

    assert store.get("llm_egress_subjects", f"acme:{ref_pan}") is not None
    assert store.get("llm_egress_subjects", f"acme:{ref_aadhaar}") is not None

    # Purge older than 1 day from t=200000 (cutoff is ~113600) -> k1 should be purged
    purged = log.purge("acme", retention_days=1)
    assert purged == 1
    assert store.get("llm_egress_subjects", f"acme:{ref_pan}") is None
    assert store.get("llm_egress_subjects", f"acme:{ref_aadhaar}") is not None

    # delete_tenant removes all remaining index entries
    deleted = log.delete_tenant("acme")
    assert deleted == 1
    assert store.get("llm_egress_subjects", f"acme:{ref_aadhaar}") is None


# --- Integration Tests: LLM Call WITHHELD outcome ----------------------------------------------------


class DummyLLM(BaseLLMClient):
    def __init__(self):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))

    async def chat(self, messages, tools=None, temperature=None):
        return LLMResponse(content="verified", usage={"input_tokens": 10, "output_tokens": 5})

    async def chat_stream(self, messages, tools=None, temperature=None):
        yield {"event": "token", "data": "verified"}
        yield {"event": "done", "data": {}}


@pytest.mark.asyncio
async def test_llm_call_with_pan_records_withheld_ref(disclosure_key):
    """LLM call with a PAN records a WITHHELD ref in the egress row and indexes it."""
    store = MemoryKeyValueStore()
    log = EgressLog(store, clock=lambda: NOW)
    gw = LLMGateway(
        config=LLMConfig(provider=LLMProvider.FAKE),
        egress_log=log,
        policy_resolver=registry_policy_resolver(load_registry(REGISTRY)),
    )
    gw.client = DummyLLM()

    msg = ChatMessage(role="user", content=f"Verify employee PAN {FAKE_PAN} please")
    res = await gw.chat([msg], tenant_id="acme")
    assert res.content == "verified"

    [record] = log.list("acme")
    assert record["channel"] == "llm"
    assert record["status"] == "COMPLETED"
    assert len(record["subject_refs"]) == 1
    sub = record["subject_refs"][0]
    expected_ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert sub["ref"] == expected_ref
    assert sub["kind"] == "PAN"
    assert sub["outcome"] == "WITHHELD"


# --- Integration Tests: MCP tools/call DISCLOSED outcome ---------------------------------------------


def _rpc_req(method: str, params: Optional[Dict[str, Any]] = None, req_id: str = "1") -> Dict[str, Any]:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


def test_mcp_tools_call_records_disclosed_refs(make_client, bearer, monkeypatch, disclosure_key):
    """MCP tools/call output containing string email and identifier scalar records DISCLOSED refs."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store, clock=lambda: NOW)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    tool_output = {
        "success": True,
        "notes": f"Notification forwarded to {FAKE_EMAIL} for review",
        "employee_name": "Tariq Al-Mansoor",
    }

    async def mock_call_tool(self, name, arguments):
        return tool_output

    monkeypatch.setattr(HrmsMcpServer, "call_tool", mock_call_tool)

    headers = {**bearer(tenant="acme", roles=["staff"]), "X-HRMS-Jurisdiction": "IN"}
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", {"name": "hrms_search_labor_regulations", "arguments": {"q": "test"}}),
        headers=headers,
    )
    assert res.status_code == 200

    rows = log.list("acme")
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "mcp"
    assert row["status"] == "COMPLETED"

    ref_email = disclosure.compute_ref("acme", "EMAIL", FAKE_EMAIL)
    ref_name = disclosure.compute_ref("acme", "NAME", "Tariq Al-Mansoor")

    row_refs = {s["ref"]: s for s in row["subject_refs"]}
    assert ref_email in row_refs
    assert row_refs[ref_email]["kind"] == "EMAIL"
    assert row_refs[ref_email]["outcome"] == "DISCLOSED"

    assert ref_name in row_refs
    assert row_refs[ref_name]["kind"] == "NAME"
    assert row_refs[ref_name]["outcome"] == "DISCLOSED"


def test_failed_mcp_call_adds_no_refs(make_client, bearer, monkeypatch, disclosure_key):
    """Failed MCP tools/call does not add any disclosure refs."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store, clock=lambda: NOW)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    async def mock_failed_tool(self, name, arguments):
        return {"success": False, "error": "FAILURE", "email": FAKE_EMAIL}

    monkeypatch.setattr(HrmsMcpServer, "call_tool", mock_failed_tool)

    headers = {**bearer(tenant="acme", roles=["staff"]), "X-HRMS-Jurisdiction": "IN"}
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", {"name": "hrms_search_labor_regulations", "arguments": {}}),
        headers=headers,
    )
    assert res.status_code == 200

    rows = log.list("acme")
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["subject_refs"] == []


# --- HTTP Tests: POST /v1/governance/disclosures/lookup (K2.4) ---------------------------------------


def test_lookup_endpoint_key_missing_or_short_returns_503(make_client, bearer, monkeypatch):
    """503 DISCLOSURE_INDEX_NOT_CONFIGURED when key file is missing, empty, or shorter than 32 bytes."""
    client = make_client()
    monkeypatch.delenv("HRMS_DISCLOSURE_KEY_FILE", raising=False)

    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"]),
    )
    assert res.status_code == 503
    assert res.json()["error"] == "DISCLOSURE_INDEX_NOT_CONFIGURED"


def test_lookup_endpoint_requires_manage_governance_and_recent_signin(make_client, bearer, disclosure_key):
    """Requires MANAGE_GOVERNANCE (403 for hr_admin/hr_manager) and recent sign-in (401 for old token)."""
    client = make_client()

    # 1. hr_manager/hr_admin lacks MANAGE_GOVERNANCE -> 403
    res_hr = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["hr_admin"]),
    )
    assert res_hr.status_code == 403

    # 2. platform_admin with stale token (issued_at > 300s ago) -> 401 REAUTH_REQUIRED
    res_stale = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"], iat=NOW - 305),
    )
    assert res_stale.status_code == 401
    assert res_stale.json()["error"] == "REAUTH_REQUIRED"

    # 3. platform_admin with fresh token -> 200
    res_fresh = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"], iat=NOW),
    )
    assert res_fresh.status_code == 200


def test_lookup_never_seen_returns_found_false(make_client, bearer, disclosure_key):
    """Lookup for a value never logged returns found: False, empty disclosures list."""
    client = make_client()
    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"]),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "PAN"
    assert data["found"] is False
    assert data["disclosures"] == []


def test_lookup_another_tenants_value_returns_found_false(make_client, bearer, monkeypatch, disclosure_key):
    """A disclosure logged for tenant 'globex' cannot be found by tenant 'acme'."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store, clock=lambda: NOW)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    ref_globex = disclosure.compute_ref("globex", "PAN", FAKE_PAN)
    log.record(
        tenant_id="globex",
        provider="fake",
        model="m1",
        payload_sha256="sha256:" + "0" * 64,
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref_globex, "kind": "PAN", "outcome": "WITHHELD"}],
    )

    # Acme queries for the same PAN -> found: False
    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(tenant="acme", roles=["platform_admin"]),
    )
    assert res.status_code == 200
    assert res.json()["found"] is False
    assert res.json()["disclosures"] == []


def test_lookup_finds_llm_and_mcp_records(make_client, bearer, monkeypatch, disclosure_key):
    """Lookup finds WITHHELD (LLM) and DISCLOSED (MCP) records."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store, clock=lambda: NOW)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)

    log.record(
        tenant_id="acme",
        provider="fake-provider",
        model="fake-model",
        payload_sha256="sha256:" + "1" * 64,
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
        status="COMPLETED",
    )

    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(tenant="acme", roles=["platform_admin"]),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "PAN"
    assert data["found"] is True
    assert len(data["disclosures"]) == 1
    d = data["disclosures"][0]
    assert d["channel"] == "llm"
    assert d["provider"] == "fake-provider"
    assert d["model"] == "fake-model"
    assert d["outcome"] == "WITHHELD"
    assert d["status"] == "COMPLETED"


def test_response_and_stored_lookup_never_contain_value_or_ref(make_client, bearer, monkeypatch, disclosure_key):
    """Response text and stored audit record never echo or contain the raw value or the ref."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store, clock=lambda: NOW)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    log.record(
        tenant_id="acme",
        provider="fake",
        model="m",
        payload_sha256="sha256:" + "0" * 64,
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
        status="COMPLETED",
    )

    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"]),
    )
    assert res.status_code == 200

    # 1. Assert raw value and ref are NOT in the HTTP response body
    assert FAKE_PAN not in res.text
    assert ref not in res.text

    # 2. Assert raw value and ref are NOT in the stored audit row in 'disclosure_lookups'
    app_store = getattr(client.app.state, "storage", None) or egress_store
    lookup_rows = app_store.get_all("disclosure_lookups")
    assert len(lookup_rows) >= 1
    for row_key, row in lookup_rows.items():
        assert FAKE_PAN not in json.dumps(row)
        assert ref not in json.dumps(row)
        assert set(row.keys()) == {"tenant_id", "user_id", "at", "kind", "found"}


def test_invalid_kind_or_empty_or_overlong_value_returns_422(make_client, bearer, disclosure_key):
    """Invalid kind, empty kind, empty value, or >256 chars value returns 422 Unprocessable Entity."""
    client = make_client()
    h = bearer(roles=["platform_admin"])

    # Invalid kind
    res_kind = client.post(
        "/v1/governance/disclosures/lookup", json={"kind": "INVALID_KIND", "value": FAKE_PAN}, headers=h
    )
    assert res_kind.status_code == 422

    # Empty kind
    res_empty_k = client.post(
        "/v1/governance/disclosures/lookup", json={"kind": "", "value": FAKE_PAN}, headers=h
    )
    assert res_empty_k.status_code == 422

    # Empty value
    res_empty_v = client.post(
        "/v1/governance/disclosures/lookup", json={"kind": "PAN", "value": ""}, headers=h
    )
    assert res_empty_v.status_code == 422

    # Value longer than 256 chars
    long_val = "A" * 257
    res_long = client.post(
        "/v1/governance/disclosures/lookup", json={"kind": "PAN", "value": long_val}, headers=h
    )
    assert res_long.status_code == 422


def test_lookup_at_most_200_results_and_newest_first(make_client, bearer, monkeypatch, disclosure_key):
    """Lookup returns at most 200 disclosures, ordered newest first."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    log = EgressLog(egress_store)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)

    # Insert 220 records with increasing timestamps
    for i in range(220):
        log._clock = lambda t=float(i): t
        log.record(
            tenant_id="acme",
            provider="fake",
            model=f"m-{i}",
            payload_sha256="sha256:" + f"{i:064x}",
            redacted_kinds={"PAN": 1},
            channel="llm",
            subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
            status="COMPLETED",
        )

    res = client.post(
        "/v1/governance/disclosures/lookup",
        json={"kind": "PAN", "value": FAKE_PAN},
        headers=bearer(roles=["platform_admin"]),
    )
    assert res.status_code == 200
    disclosures = res.json()["disclosures"]
    assert len(disclosures) == 200

    # Verify descending order (newest first)
    timestamps = [d["at"] for d in disclosures]
    assert timestamps == sorted(timestamps, reverse=True)
    assert timestamps[0] == 219.0


# --- Follow-up Review Tests: Encapsulation, Concurrency, Key Caching ---------------------------------


def test_egress_log_find_subject_and_record_lookup(disclosure_key):
    """find_subject returns disclosure dicts newest-first up to limit for caller tenant only;
    record_lookup stores audit row in disclosure_lookups with clock."""
    store = MemoryKeyValueStore()
    t = 1000.0
    log = EgressLog(store, clock=lambda: t)

    ref_pan = disclosure.compute_ref("acme", "PAN", FAKE_PAN)

    # 1. Record 3 rows for acme with ref_pan at t=1000, 1010, 1020
    t = 1000.0
    log.record(
        tenant_id="acme",
        provider="openai",
        model="gpt-4o",
        payload_sha256="sha256:1",
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref_pan, "kind": "PAN", "outcome": "WITHHELD"}],
        status="COMPLETED",
    )
    t = 1010.0
    log.record(
        tenant_id="acme",
        provider="anthropic",
        model="claude-3-5-sonnet",
        payload_sha256="sha256:2",
        redacted_kinds={"PAN": 1},
        channel="mcp",
        subject_refs=[{"ref": ref_pan, "kind": "PAN", "outcome": "DISCLOSED"}],
        status="COMPLETED",
    )
    t = 1020.0
    log.record(
        tenant_id="acme",
        provider="openai",
        model="gpt-4o",
        payload_sha256="sha256:3",
        redacted_kinds={"PAN": 1},
        channel="voice",
        subject_refs=[{"ref": ref_pan, "kind": "PAN", "outcome": "WITHHELD"}],
        status="FAILED",
    )

    # 2. Record 1 row for globex with the same ref_pan (should NOT be returned for acme)
    t = 1030.0
    log.record(
        tenant_id="globex",
        provider="openai",
        model="gpt-4o",
        payload_sha256="sha256:4",
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": ref_pan, "kind": "PAN", "outcome": "WITHHELD"}],
        status="COMPLETED",
    )

    # 3. Call find_subject with limit=200
    res = log.find_subject("acme", ref_pan, limit=200)
    assert len(res) == 3
    # Check ordering: newest first
    assert [r["at"] for r in res] == [1020.0, 1010.0, 1000.0]
    # Check exact dictionary shape
    assert res[0] == {
        "at": 1020.0,
        "channel": "voice",
        "provider": "openai",
        "model": "gpt-4o",
        "outcome": "WITHHELD",
        "status": "FAILED",
    }
    assert res[1]["outcome"] == "DISCLOSED"
    assert res[1]["channel"] == "mcp"

    # 4. Limit parameter respects ceiling
    limited = log.find_subject("acme", ref_pan, limit=2)
    assert len(limited) == 2
    assert [r["at"] for r in limited] == [1020.0, 1010.0]

    # 5. Non-existent ref returns empty list
    empty = log.find_subject("acme", "hmac-sha256:nonexistent", limit=200)
    assert empty == []

    # 6. Test record_lookup
    t = 2000.0
    log.record_lookup("acme", "admin-1", "PAN", True)
    t = 2005.0
    log.record_lookup("acme", "admin-2", "EMAIL", False)

    lookup_rows = store.get_all("disclosure_lookups")
    assert len(lookup_rows) == 2
    items = sorted(lookup_rows.values(), key=lambda x: x["at"])
    assert items[0] == {
        "tenant_id": "acme",
        "user_id": "admin-1",
        "at": 2000.0,
        "kind": "PAN",
        "found": True,
    }
    assert items[1] == {
        "tenant_id": "acme",
        "user_id": "admin-2",
        "at": 2005.0,
        "kind": "EMAIL",
        "found": False,
    }
    # No value or ref stored
    for row in items:
        assert "value" not in row
        assert "ref" not in row


def test_app_does_not_import_egress_namespaces():
    """Route must not touch egress_log._store or import SUBJECT_NAMESPACE/EGRESS_NAMESPACE directly."""
    import hrms_plugin.server.app as app_mod

    assert not hasattr(app_mod, "SUBJECT_NAMESPACE")
    assert not hasattr(app_mod, "EGRESS_NAMESPACE")


def test_concurrent_recording_same_ref_finds_all_entries(disclosure_key):
    """50 threads each recording a row with the same ref concurrently -> find_subject returns 50 entries."""
    import threading

    store = MemoryKeyValueStore()
    log = EgressLog(store)
    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)

    def record_worker(worker_id: int):
        log.record(
            tenant_id="acme",
            provider="test-prov",
            model=f"model-{worker_id}",
            payload_sha256=f"sha256:{worker_id:064x}",
            redacted_kinds={"PAN": 1},
            channel="llm",
            subject_refs=[{"ref": ref, "kind": "PAN", "outcome": "WITHHELD"}],
            status="COMPLETED",
        )

    threads = [threading.Thread(target=record_worker, args=(i,)) for i in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    entries = log.find_subject("acme", ref, limit=200)
    assert len(entries) == 50


def test_get_disclosure_key_caches_unchanged_file(tmp_path, monkeypatch):
    """get_disclosure_key must not read the file on every call; cache keyed by (path, mtime_ns, size)."""
    from pathlib import Path

    key_file = tmp_path / "cache_test.key"
    key_bytes = b"0" * 32
    key_file.write_bytes(key_bytes)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))

    read_calls = 0
    orig_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        nonlocal read_calls
        read_calls += 1
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    # First read
    k1 = disclosure.get_disclosure_key()
    assert k1 == key_bytes
    assert read_calls == 1

    # Second read: unchanged file -> cached, read_bytes NOT called again
    k2 = disclosure.get_disclosure_key()
    assert k2 == key_bytes
    assert read_calls == 1

    # compute_ref also uses cached key without extra read_bytes
    ref = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert ref is not None
    assert read_calls == 1


def test_rotating_disclosure_key_file_changes_ref(tmp_path, monkeypatch):
    """Replacing the key file with a different 32-byte key is picked up and changes ref."""
    import time

    key_file = tmp_path / "rotate_test.key"
    key1 = b"1" * 32
    key_file.write_bytes(key1)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))

    ref1 = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert ref1 is not None

    # Replace with a different 32-byte key; ensure mtime changes
    key2 = b"2" * 32
    time.sleep(0.01)
    key_file.write_bytes(key2)

    ref2 = disclosure.compute_ref("acme", "PAN", FAKE_PAN)
    assert ref2 is not None
    assert ref1 != ref2


def test_deleting_key_file_marks_not_configured(tmp_path, monkeypatch):
    """Deleting the key file makes disclosure not configured immediately."""
    key_file = tmp_path / "delete_test.key"
    key_file.write_bytes(b"3" * 32)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))

    assert disclosure.is_configured() is True
    assert disclosure.get_disclosure_key() is not None

    key_file.unlink()
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False


def test_missing_or_unreadable_file_catches_oserror_only(tmp_path, monkeypatch):
    """get_disclosure_key catches OSError only, not general Exception."""
    from pathlib import Path

    key_file = tmp_path / "oserror_test.key"
    key_file.write_bytes(b"4" * 32)
    monkeypatch.setenv("HRMS_DISCLOSURE_KEY_FILE", str(key_file))

    def raise_oserror(self):
        raise OSError("Disk I/O error")

    monkeypatch.setattr(Path, "stat", raise_oserror)
    assert disclosure.get_disclosure_key() is None
    assert disclosure.is_configured() is False

    def raise_runtime_error(self):
        raise RuntimeError("Unexpected boom")

    monkeypatch.setattr(Path, "stat", raise_runtime_error)
    with pytest.raises(RuntimeError):
        disclosure.get_disclosure_key()


def test_record_computes_is_configured_once(monkeypatch):
    """record() should compute is_configured once per call."""
    store = MemoryKeyValueStore()
    log = EgressLog(store)

    calls = 0
    orig_is_configured = disclosure.is_configured

    def counting_is_configured():
        nonlocal calls
        calls += 1
        return orig_is_configured()

    monkeypatch.setattr(disclosure, "is_configured", counting_is_configured)

    log.record(
        tenant_id="acme",
        provider="fake",
        model="m-1",
        payload_sha256="sha256:abc",
        redacted_kinds={"PAN": 1},
        channel="llm",
        subject_refs=[{"ref": "hmac-sha256:abc", "kind": "PAN", "outcome": "WITHHELD"}],
        status="COMPLETED",
    )
    assert calls == 1

