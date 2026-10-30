"""Adversarial audit tests for writeback proposals, rollback lifecycle, payroll fingerprint approvals,
requisition shape approvals, and Ed25519 audit attribution signing.

Covers:
- Writeback two-person approval rule: proposer cannot approve (403 SAME_PERSON)
- Writeback 24-hour expiry on proposals and rollback requests
- Writeback rollback two-step workflow (202 ROLLBACK_PENDING -> 403 SAME_PERSON -> 200 ROLLED_BACK)
- Payroll Salary Slip fingerprint approval two-person rule (202 PENDING_CONFIRMATION -> 403 SAME_PERSON -> 200 APPROVED)
- Question Q2: Non-idempotent shape/fingerprint approval (returns 202 PENDING_CONFIRMATION on re-approval)
- Requisition shape approval guards (409 FINGERPRINT_NOT_OBSERVED, 403 SAME_PERSON)
- Ed25519 audit header signing, verification, and tamper detection across all canonical headers
- Audit signing fails closed when key is missing/unconfigured (503 AUDIT_SIGNING_NOT_CONFIGURED)
"""

import pytest
import yaml

from hrms_plugin.security.audit import (
    AUDIT_HEADER_NAMES,
    AuditSigningError,
    HostAuditGateway,
    verify_audit_headers,
)
from hrms_plugin.server import app as app_mod


@pytest.fixture(autouse=True)
def hosts_env(monkeypatch, tmp_path):
    """Session/module fixture setting up HRMS_HOSTS for acme tenant."""
    hosts_data = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://hr.acme.test",
                "api_key_env": "ACME_FRAPPE_KEY",
                "api_secret_env": "ACME_FRAPPE_SECRET",
                "timeout_seconds": 10,
                "writable_fields": {
                    "Employee": {
                        "notice_number_of_days": "int",
                    }
                },
            }
        ]
    }
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(hosts_data))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "test-key")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "test-secret")
    return path


@pytest.fixture
def mock_writeback_host(monkeypatch):
    """Mock external Frappe client for writeback and rollback operations."""
    records = {
        "Employee:HR-EMP-001": {
            "name": "HR-EMP-001",
            "doctype": "Employee",
            "notice_number_of_days": 30,
            "modified": "2026-10-30 10:00:00.000000",
        }
    }

    class FakeFrappeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_document(self, doctype, name):
            key = f"{doctype}:{name}"
            if key not in records:
                from hrms_plugin.writeback.service import WritebackError
                raise WritebackError("NOT_FOUND", f"{doctype} {name} not found")
            return dict(records[key])

        async def update_document(self, doctype, name, patch, headers=None):
            key = f"{doctype}:{name}"
            if key not in records:
                from hrms_plugin.writeback.service import WritebackError
                raise WritebackError("NOT_FOUND", f"{doctype} {name} not found")
            records[key].update(patch)
            records[key]["modified"] = f"2026-10-30 10:05:00.{len(records[key])}"
            return dict(records[key])

        async def aclose(self):
            pass

    monkeypatch.setattr(
        app_mod.writeback_service,
        "_client_factory",
        lambda host, key, secret: FakeFrappeClient(),
    )
    return records


def test_writeback_two_person_rule_and_expiry(make_client, bearer, clock, mock_writeback_host, monkeypatch):
    """Positive & Negative: Writeback proposal requires two distinct persons; expires after 24h."""
    monkeypatch.setattr(app_mod.writeback_service, "_clock", clock)
    client = make_client()
    h_proposer = bearer(tenant="acme", roles=["hr_manager"], sub="user-proposer")

    # 1. Propose change
    body = {
        "doctype": "Employee",
        "name": "HR-EMP-001",
        "patch": {"notice_number_of_days": 60},
    }
    prop_res = client.post("/v1/writeback/proposals", json=body, headers=h_proposer)
    assert prop_res.status_code == 200
    pid = prop_res.json()["id"]
    assert prop_res.json()["status"] == "PENDING"

    # 2. Same person cannot approve (403 SAME_PERSON)
    same_res = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=h_proposer)
    assert same_res.status_code == 403
    assert same_res.json()["error"] == "SAME_PERSON"

    # 3. Expiry test: advancing clock past 24 hours marks proposal EXPIRED
    clock.now += 86401
    h_approver_fresh = bearer(tenant="acme", roles=["hr_manager"], sub="user-approver", iat=clock.now)
    exp_res = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=h_approver_fresh)
    assert exp_res.status_code == 409
    assert exp_res.json()["error"] == "EXPIRED"

    # 4. Propose again at current clock and approve with different user
    h_proposer_fresh = bearer(tenant="acme", roles=["hr_manager"], sub="user-proposer", iat=clock.now)
    prop_res2 = client.post("/v1/writeback/proposals", json=body, headers=h_proposer_fresh)
    assert prop_res2.status_code == 200
    pid2 = prop_res2.json()["id"]

    ok_res = client.post(f"/v1/writeback/proposals/{pid2}/approve", headers=h_approver_fresh)
    assert ok_res.status_code == 200
    assert ok_res.json()["status"] == "EXECUTED"
    assert ok_res.json()["approved_by"] == "user-approver"
    assert "audit" in ok_res.json()


def test_writeback_rollback_two_person_rule(make_client, bearer, mock_writeback_host):
    """Positive & Negative: Rollback requires a 2-step confirmation with 2 different persons."""
    client = make_client()
    h_user1 = bearer(tenant="acme", roles=["hr_manager"], sub="user-1")
    h_user2 = bearer(tenant="acme", roles=["hr_manager"], sub="user-2")
    h_user3 = bearer(tenant="acme", roles=["hr_manager"], sub="user-3")

    body = {
        "doctype": "Employee",
        "name": "HR-EMP-001",
        "patch": {"notice_number_of_days": 45},
    }
    pid = client.post("/v1/writeback/proposals", json=body, headers=h_user1).json()["id"]
    client.post(f"/v1/writeback/proposals/{pid}/approve", headers=h_user2)

    # Step 1: User 1 initiates rollback -> 202 ROLLBACK_PENDING
    rb1 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=h_user1)
    assert rb1.status_code == 202
    assert rb1.json()["status"] == "ROLLBACK_PENDING"

    # Same user calling rollback again -> 403 SAME_PERSON
    rb_same = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=h_user1)
    assert rb_same.status_code == 403
    assert rb_same.json()["error"] == "SAME_PERSON"

    # Step 2: Different user confirms rollback -> 200 ROLLED_BACK
    rb2 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=h_user3)
    assert rb2.status_code == 200
    assert rb2.json()["status"] == "ROLLED_BACK"


def test_payroll_fingerprint_approval_two_person_and_question_q2(make_client, bearer, clock):
    """Positive: Two-person approval for Salary Slip fingerprint.
    Question Q2: documents that re-approving an already approved fingerprint returns 202 PENDING_CONFIRMATION.
    """
    client = make_client()
    h_admin1 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    h_admin2 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-2")
    valid_fp = "sha256:" + "a" * 64

    # Step 1: Propose fingerprint -> 202 PENDING_CONFIRMATION
    res1 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": valid_fp},
        headers=h_admin1,
    )
    assert res1.status_code == 202
    assert res1.json()["status"] == "PENDING_CONFIRMATION"

    # Same person calling again -> 403 SAME_PERSON
    res_same = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": valid_fp},
        headers=h_admin1,
    )
    assert res_same.status_code == 403
    assert res_same.json()["error"] == "SAME_PERSON"

    # Step 2: Second person confirms -> 200 APPROVED
    res2 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": valid_fp},
        headers=h_admin2,
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "APPROVED"

    # Question Q2 Behavior: A third call for the same fingerprint resets to 202 PENDING_CONFIRMATION
    res_reapprove = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": valid_fp},
        headers=h_admin1,
    )
    assert res_reapprove.status_code == 202
    assert res_reapprove.json()["status"] == "PENDING_CONFIRMATION"


def test_requisition_shape_approval_guards(make_client, bearer):
    """Negative & Positive: Shape approval guards on observed fingerprint and same-person rule."""
    client = make_client()
    h_admin1 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    h_admin2 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-2")
    test_fp = "sha256:" + "b" * 64

    # 1. Non-observed fingerprint -> 409 FINGERPRINT_NOT_OBSERVED
    unobserved = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": test_fp},
        headers=h_admin1,
    )
    assert unobserved.status_code == 409
    assert unobserved.json()["error"] == "FINGERPRINT_NOT_OBSERVED"

    # 2. Record observed shape in storage
    from hrms_plugin.server.app import _shape_key, _storage
    _storage.set("observed_shapes", _shape_key("acme", "frappe"), {"fingerprint": test_fp, "shape": {"title": "str"}})

    # 3. First approval -> 202 PENDING_CONFIRMATION
    res1 = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": test_fp},
        headers=h_admin1,
    )
    assert res1.status_code == 202
    assert res1.json()["status"] == "PENDING_CONFIRMATION"

    # 4. Same person approval -> 403 SAME_PERSON
    res_same = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": test_fp},
        headers=h_admin1,
    )
    assert res_same.status_code == 403
    assert res_same.json()["error"] == "SAME_PERSON"

    # 5. Second person approval -> 200 APPROVED
    res2 = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": test_fp},
        headers=h_admin2,
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "APPROVED"


def test_ed25519_audit_attribution_signing_and_tampering(audit_signing_env):
    """Positive & Negative: Ed25519 signature verifies; tampering with ANY header fails verification."""
    priv_key = audit_signing_env["private_key"]
    pub_key = priv_key.public_key()
    gateway = HostAuditGateway(
        key_file=audit_signing_env["key_file"],
        key_id=audit_signing_env["key_id"],
    )

    headers = gateway.create_attribution_headers(
        tenant_id="acme",
        agent_name="WritebackService",
        hitl_approver_id="approver-42",
        reasoning_payload={"op": "update", "doctype": "Employee", "id": "EMP-1"},
    )
    http_headers = headers.to_http_headers()

    # 1. Positive: untampered headers pass verification
    assert verify_audit_headers(http_headers, pub_key) is True

    # 2. Tampering test: tamper each of the 8 canonical headers individually
    for header_name in AUDIT_HEADER_NAMES:
        tampered = dict(http_headers)
        orig_val = tampered[header_name]
        tampered[header_name] = orig_val + "_tampered"
        assert verify_audit_headers(tampered, pub_key) is False, f"Tampering {header_name} was not detected!"

    # 3. Tampering signature bytes fails verification
    tampered_sig = dict(http_headers)
    sig = tampered_sig["X-Audit-Signature"]
    tampered_sig["X-Audit-Signature"] = ("B" if sig.startswith("A") else "A") + sig[1:]
    assert verify_audit_headers(tampered_sig, pub_key) is False

    # 4. Missing signature fails verification
    no_sig = dict(http_headers)
    del no_sig["X-Audit-Signature"]
    assert verify_audit_headers(no_sig, pub_key) is False


def test_ed25519_audit_unconfigured_fails_closed(monkeypatch):
    """Negative: Missing or unconfigured audit key fails closed with AUDIT_SIGNING_NOT_CONFIGURED."""
    monkeypatch.delenv("HRMS_AUDIT_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("HRMS_AUDIT_KEY_ID", raising=False)
    gateway = HostAuditGateway(key_file=None, key_id=None)

    assert gateway.is_configured() is False
    with pytest.raises(AuditSigningError) as exc_info:
        gateway.create_attribution_headers("acme", "Agent", "user-1", {})
    assert exc_info.value.code == "AUDIT_SIGNING_NOT_CONFIGURED"
