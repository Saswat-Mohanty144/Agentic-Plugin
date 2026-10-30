"""Unit tests for Gap E: Decisions D6 (two-person rule) and D7 (Ed25519 audit header signing)."""

from __future__ import annotations

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hrms_plugin.integrations.frappe import FrappeClient
from hrms_plugin.security import audit as audit_module
from hrms_plugin.security.audit import HostAuditGateway
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from hrms_plugin.writeback.service import WritebackError, WritebackService
from tests.unit.test_declarative_connector import ENV, ICEIPTS_HOST, Http, _patch_transport, iceipts_page, iceipts_row

HOSTS = {
    "tenants": [
        {
            "tenant_id": "acme",
            "vendor": "frappe",
            "base_url": "https://hr.acme.test",
            "api_key_env": "ACME_FRAPPE_KEY",
            "api_secret_env": "ACME_FRAPPE_SECRET",
            "timeout_seconds": 10,
            "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
        }
    ]
}

USER_1 = AuthContext(
    user_id="user-1",
    tenant_id="acme",
    roles=frozenset({UserRole.HR_ADMIN}),
)
USER_2 = AuthContext(
    user_id="user-2",
    tenant_id="acme",
    roles=frozenset({UserRole.HR_ADMIN}),
)
APPROVER = AuthContext(
    user_id="user-approver",
    tenant_id="acme",
    roles=frozenset({UserRole.HR_ADMIN}),
)


class StatefulFrappeMock:
    def __init__(self):
        self.doc = {
            "name": "HR-EMP-00001",
            "employee_name": "Sunita Verma",
            "notice_number_of_days": 30,
            "modified": "2026-10-01 10:00:00.000001",
        }
        self.version = 1
        self.puts = []

    def touch(self):
        self.version += 1
        self.doc["modified"] = f"2026-10-01 10:00:00.{self.version:06d}"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/resource/Employee/HR-EMP-00001":
            return httpx.Response(404, json={})
        if request.method == "GET":
            return httpx.Response(200, json={"data": dict(self.doc)})
        if request.method == "PUT":
            body = __import__("json").loads(request.content)
            self.puts.append({"body": body, "headers": dict(request.headers)})
            self.doc.update(body)
            self.touch()
            return httpx.Response(200, json={"data": dict(self.doc)})
        return httpx.Response(405, json={})


@pytest.fixture
def test_ed25519_key(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "test_audit_key.pem"
    key_file.write_bytes(pem)
    key_id = "test-audit-key-id-42"
    monkeypatch.setenv("HRMS_AUDIT_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("HRMS_AUDIT_KEY_ID", key_id)
    return {"priv": priv, "pub": priv.public_key(), "file": str(key_file), "key_id": key_id}


@pytest.fixture
def mock_frappe():
    return StatefulFrappeMock()


@pytest.fixture
def wb_service(tmp_path, monkeypatch, mock_frappe, test_ed25519_key):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    clock = {"now": 1_800_000_000.0}

    def factory(host, key, secret):
        return FrappeClient(
            host, key, secret, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_frappe.handler))
        )

    store = MemoryKeyValueStore()
    svc = WritebackService(store=store, client_factory=factory, clock=lambda: clock["now"])
    svc.test_clock = clock
    svc.mock_frappe = mock_frappe
    return svc


# ==============================================================================
# D6 Part 1: Two-person rollback (Service level)
# ==============================================================================


async def test_rollback_two_person_rule_service_lifecycle(wb_service):
    """Positive: First user requests rollback -> 202/ROLLBACK_PENDING; second user executes -> ROLLED_BACK."""
    p = await wb_service.propose(USER_1, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await wb_service.approve(APPROVER, p["id"])

    # Step 1: User 1 requests rollback
    first_res = await wb_service.rollback(USER_1, p["id"])
    assert first_res["status"] == "ROLLBACK_PENDING"
    assert first_res["rollback_requested_by"] == "user-1"
    assert first_res.get("rollback_requested_at") is not None

    # Stored proposal retains status EXECUTED plus rollback_requested_by
    stored = await wb_service.get(USER_1, p["id"])
    assert stored["status"] == "EXECUTED"
    assert stored["rollback_requested_by"] == "user-1"

    # Step 1b (Negative): Same user tries again -> SAME_PERSON
    with pytest.raises(WritebackError) as exc_info:
        await wb_service.rollback(USER_1, p["id"])
    assert exc_info.value.code == "SAME_PERSON"

    # Step 2: Different authorised user executes rollback
    second_res = await wb_service.rollback(USER_2, p["id"])
    assert second_res["status"] == "ROLLED_BACK"
    assert second_res["rollback_requested_by"] == "user-1"
    assert second_res["rolled_back_by"] == "user-2"
    assert wb_service.mock_frappe.doc["notice_number_of_days"] == 30

    # Step 3 (Idempotency): Third call returns ROLLED_BACK without extra host PUT
    puts_before = len(wb_service.mock_frappe.puts)
    third_res = await wb_service.rollback(USER_2, p["id"])
    assert third_res["status"] == "ROLLED_BACK"
    assert len(wb_service.mock_frappe.puts) == puts_before


async def test_rollback_two_person_invalid_state_and_conflicts(wb_service):
    """Negative & Boundary: Rollback of pending proposal, and host conflict at execution."""
    p = await wb_service.propose(USER_1, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})

    # Cannot rollback PENDING proposal
    with pytest.raises(WritebackError) as exc_info:
        await wb_service.rollback(USER_1, p["id"])
    assert exc_info.value.code == "INVALID_STATE"

    # Approve proposal
    await wb_service.approve(APPROVER, p["id"])

    # User 1 requests rollback
    req_res = await wb_service.rollback(USER_1, p["id"])
    assert req_res["status"] == "ROLLBACK_PENDING"

    # External modification on host before execution
    wb_service.mock_frappe.doc["notice_number_of_days"] = 90
    wb_service.mock_frappe.touch()

    # User 2 attempts execution -> ROLLBACK_CONFLICT
    with pytest.raises(WritebackError) as exc_info2:
        await wb_service.rollback(USER_2, p["id"])
    assert exc_info2.value.code == "ROLLBACK_CONFLICT"


# ==============================================================================
# D6 Part 2: Two-person rollback (HTTP level)
# ==============================================================================


def test_rollback_http_two_person_flow(make_client, bearer, monkeypatch, tmp_path, mock_frappe, test_ed25519_key):
    """HTTP: First call returns 202 ROLLBACK_PENDING, same user 403 SAME_PERSON, second user 200 ROLLED_BACK."""
    from hrms_plugin.server import app as app_mod

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    monkeypatch.setattr(
        app_mod.writeback_service,
        "_client_factory",
        lambda h, k, s: FrappeClient(
            h, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_frappe.handler))
        ),
    )

    client = make_client()
    body = {"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 60}}
    prop = client.post("/v1/writeback/proposals", json=body, headers=bearer(sub="user-1"))
    assert prop.status_code == 200
    pid = prop.json()["id"]

    appr = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=bearer(sub="user-approver"))
    assert appr.status_code == 200

    # Step 1: User 1 requests rollback -> 202 ROLLBACK_PENDING
    rb1 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="user-1"))
    assert rb1.status_code == 202
    assert rb1.json()["status"] == "ROLLBACK_PENDING"
    assert rb1.json()["rollback_requested_by"] == "user-1"

    # Step 1b: User 1 calls again -> 403 SAME_PERSON
    rb_same = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="user-1"))
    assert rb_same.status_code == 403
    assert rb_same.json()["error"] == "SAME_PERSON"

    # Step 2: User 2 confirms rollback -> 200 ROLLED_BACK
    rb2 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="user-2"))
    assert rb2.status_code == 200
    assert rb2.json()["status"] == "ROLLED_BACK"
    assert rb2.json()["rolled_back_by"] == "user-2"


# ==============================================================================
# D6 Part 3: Two-person schema fingerprint approval
# ==============================================================================


def test_schema_fingerprint_approval_two_person_rule(make_client, bearer, test_ed25519_key):
    """POST /v1/schema/fingerprints/approve: 202 PENDING_CONFIRMATION -> 403 SAME_PERSON -> 200 APPROVED."""
    client = make_client()
    fp_a = "sha256:" + "a" * 64
    body_a = {"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp_a}

    # Step 1: User 1 requests approval -> 202 PENDING_CONFIRMATION
    r1 = client.post(
        "/v1/schema/fingerprints/approve", json=body_a, headers=bearer(sub="admin-1", roles=["platform_admin"])
    )
    assert r1.status_code == 202
    assert r1.json()["status"] == "PENDING_CONFIRMATION"
    assert r1.json()["fingerprint"] == fp_a
    assert r1.json()["requested_by"] == "admin-1"

    # Step 1b: User 1 again -> 403 SAME_PERSON
    r_same = client.post(
        "/v1/schema/fingerprints/approve", json=body_a, headers=bearer(sub="admin-1", roles=["platform_admin"])
    )
    assert r_same.status_code == 403
    assert r_same.json()["error"] == "SAME_PERSON"

    # Step 2: User 2 confirms -> 200 APPROVED
    r2 = client.post(
        "/v1/schema/fingerprints/approve", json=body_a, headers=bearer(sub="admin-2", roles=["platform_admin"])
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "APPROVED"
    assert r2.json()["fingerprint"] == fp_a
    assert r2.json()["approved_by"] == "admin-2"


def test_schema_fingerprint_replacement_of_pending(make_client, bearer, test_ed25519_key):
    """Requesting a different fingerprint replaces the pending one."""
    client = make_client()
    fp_a = "sha256:" + "a" * 64
    fp_b = "sha256:" + "b" * 64

    # User 1 requests fingerprint A
    r1 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp_a},
        headers=bearer(sub="admin-1", roles=["platform_admin"]),
    )
    assert r1.status_code == 202

    # User 1 requests fingerprint B -> replaces pending A
    r2 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp_b},
        headers=bearer(sub="admin-1", roles=["platform_admin"]),
    )
    assert r2.status_code == 202
    assert r2.json()["status"] == "PENDING_CONFIRMATION"
    assert r2.json()["fingerprint"] == fp_b

    # User 2 confirms fingerprint B -> 200 APPROVED
    r3 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp_b},
        headers=bearer(sub="admin-2", roles=["platform_admin"]),
    )
    assert r3.status_code == 200
    assert r3.json()["fingerprint"] == fp_b


# ==============================================================================
# D6 Part 4: Two-person requisition shape approval
# ==============================================================================


@pytest.fixture
def requisition_env(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [{**ICEIPTS_HOST, "tenant_id": "acme"}]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("GLOBEX_TOKEN", ENV["GLOBEX_TOKEN"])
    monkeypatch.setattr(app_module, "mapping_store", MappingStore(backend=MemoryKeyValueStore()))
    store = MemoryKeyValueStore()
    monkeypatch.setattr(app_module, "_storage", store)
    return monkeypatch, store


def test_requisition_shape_approval_two_person_rule(make_client, bearer, requisition_env, test_ed25519_key):
    """POST /v1/hosts/requisitions/shape/approve: 202 PENDING_CONFIRMATION -> 403 SAME_PERSON -> 200 APPROVED."""
    monkeypatch, _ = requisition_env
    _patch_transport(monkeypatch, Http(iceipts_page([iceipts_row()])))
    client = make_client()

    # Observe the shape first
    get_res = client.get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    fp = get_res.json()["schema"]["fingerprint"]

    # Negative boundary: Non-observed fingerprint rejected with 409
    bad_fp = "sha256:" + "0" * 64
    r_bad = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": bad_fp},
        headers=bearer(sub="admin-1", roles=["platform_admin"]),
    )
    assert r_bad.status_code == 409
    assert r_bad.json()["error"] == "FINGERPRINT_NOT_OBSERVED"

    # Step 1: User 1 requests shape approval -> 202 PENDING_CONFIRMATION
    r1 = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": fp},
        headers=bearer(sub="admin-1", roles=["platform_admin"]),
    )
    assert r1.status_code == 202
    assert r1.json()["status"] == "PENDING_CONFIRMATION"
    assert r1.json()["fingerprint"] == fp
    assert r1.json()["requested_by"] == "admin-1"

    # Step 1b: User 1 again -> 403 SAME_PERSON
    r_same = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": fp},
        headers=bearer(sub="admin-1", roles=["platform_admin"]),
    )
    assert r_same.status_code == 403
    assert r_same.json()["error"] == "SAME_PERSON"

    # Step 2: User 2 confirms -> 200 APPROVED
    r2 = client.post(
        "/v1/hosts/requisitions/shape/approve",
        json={"fingerprint": fp},
        headers=bearer(sub="admin-2", roles=["platform_admin"]),
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "APPROVED"
    assert r2.json()["fingerprint"] == fp


# ==============================================================================
# D7: Ed25519 signing and verification of audit headers
# ==============================================================================


def test_audit_gateway_ed25519_signing_and_verification(test_ed25519_key):
    """Positive: HostAuditGateway signs headers with Ed25519 and verify_audit_headers validates."""
    gateway = HostAuditGateway()
    headers = gateway.create_attribution_headers(
        tenant_id="acme",
        agent_name="WritebackService",
        hitl_approver_id="user-approver",
        reasoning_payload={"action": "test", "val": 123},
    )
    http_dict = headers.to_http_headers()

    assert http_dict["X-Audit-Key-Id"] == test_ed25519_key["key_id"]
    assert "X-Audit-Signature" in http_dict
    sig = http_dict["X-Audit-Signature"]
    assert "=" not in sig  # base64url, no padding

    verify_audit_headers = getattr(audit_module, "verify_audit_headers", None)
    assert verify_audit_headers is not None, "verify_audit_headers must be defined in hrms_plugin.security.audit"

    # Verify with Ed25519PublicKey object
    assert verify_audit_headers(http_dict, test_ed25519_key["pub"]) is True

    # Verify with raw 32 bytes public key
    raw_pub = test_ed25519_key["pub"].public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert verify_audit_headers(http_dict, raw_pub) is True

    # Verify with PEM bytes public key
    pem_pub = test_ed25519_key["pub"].public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert verify_audit_headers(http_dict, pem_pub) is True
    assert verify_audit_headers(http_dict, pem_pub.decode("utf-8")) is True


def test_verify_audit_headers_tampering_and_boundaries(test_ed25519_key):
    """Negative & Boundary: Tampering with header values, signature, or key causes verify to return False."""
    gateway = HostAuditGateway()
    headers = gateway.create_attribution_headers(
        tenant_id="acme",
        agent_name="WritebackService",
        hitl_approver_id="user-approver",
        reasoning_payload={"action": "test"},
    )
    http_dict = headers.to_http_headers()
    pub = test_ed25519_key["pub"]
    verify_fn = getattr(audit_module, "verify_audit_headers", None)
    assert verify_fn is not None, "verify_audit_headers must be defined"

    # 1. Tamper header value
    tampered_tenant = dict(http_dict)
    tampered_tenant["X-Tenant-Id"] = "globex"
    assert verify_fn(tampered_tenant, pub) is False

    # 2. Tamper approver
    tampered_approver = dict(http_dict)
    tampered_approver["X-HITL-Approver-Id"] = "attacker"
    assert verify_fn(tampered_approver, pub) is False

    # 3. Tamper signature bytes
    tampered_sig = dict(http_dict)
    tampered_sig["X-Audit-Signature"] = "A" + http_dict["X-Audit-Signature"][1:]
    assert verify_fn(tampered_sig, pub) is False

    # 4. Missing signature
    no_sig = dict(http_dict)
    del no_sig["X-Audit-Signature"]
    assert verify_fn(no_sig, pub) is False

    # 5. Wrong public key
    other_priv = Ed25519PrivateKey.generate()
    assert verify_fn(http_dict, other_priv.public_key()) is False

    # 6. Invalid non-base64 signature string
    corrupt_sig = dict(http_dict)
    corrupt_sig["X-Audit-Signature"] = "???not-base64!!!"
    assert verify_fn(corrupt_sig, pub) is False


# ==============================================================================
# D7: Fail-closed on missing or unreadable audit signing key
# ==============================================================================


def test_writeback_approve_refuses_when_audit_signing_not_configured(
    make_client, bearer, monkeypatch, tmp_path, mock_frappe
):
    """POST /v1/writeback/proposals/{id}/approve returns 503 AUDIT_SIGNING_NOT_CONFIGURED before touching host."""
    from hrms_plugin.server import app as app_mod

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    monkeypatch.setattr(
        app_mod.writeback_service,
        "_client_factory",
        lambda h, k, s: FrappeClient(
            h, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_frappe.handler))
        ),
    )

    client = make_client()
    body = {"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 60}}
    prop = client.post("/v1/writeback/proposals", json=body, headers=bearer(sub="user-1"))
    assert prop.status_code == 200
    pid = prop.json()["id"]

    # Unset signing key env var
    monkeypatch.delenv("HRMS_AUDIT_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("HRMS_AUDIT_KEY_ID", raising=False)

    puts_before = len(mock_frappe.puts)
    resp = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=bearer(sub="user-approver"))
    assert resp.status_code == 503
    assert resp.json()["error"] == "AUDIT_SIGNING_NOT_CONFIGURED"
    # Ensure host was NOT contacted
    assert len(mock_frappe.puts) == puts_before


def test_writeback_rollback_refuses_when_audit_signing_not_configured(
    make_client, bearer, monkeypatch, tmp_path, mock_frappe, test_ed25519_key
):
    """POST /v1/writeback/proposals/{id}/rollback execution refuses 503 AUDIT_SIGNING_NOT_CONFIGURED
    before host write."""
    from hrms_plugin.server import app as app_mod

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    monkeypatch.setattr(
        app_mod.writeback_service,
        "_client_factory",
        lambda h, k, s: FrappeClient(
            h, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock_frappe.handler))
        ),
    )

    client = make_client()
    body = {"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 60}}
    prop = client.post("/v1/writeback/proposals", json=body, headers=bearer(sub="user-1"))
    pid = prop.json()["id"]
    appr = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=bearer(sub="user-approver"))
    assert appr.status_code == 200

    # User 1 requests rollback -> 202 ROLLBACK_PENDING
    rb1 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="user-1"))
    assert rb1.status_code == 202

    # Unset signing key before execution (step 2)
    monkeypatch.delenv("HRMS_AUDIT_SIGNING_KEY_FILE", raising=False)

    puts_before = len(mock_frappe.puts)
    rb2 = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="user-2"))
    assert rb2.status_code == 503
    assert rb2.json()["error"] == "AUDIT_SIGNING_NOT_CONFIGURED"
    assert len(mock_frappe.puts) == puts_before


def test_remediate_execute_refuses_when_audit_signing_not_configured(make_client, bearer, monkeypatch):
    """POST /v1/remediate/execute returns 503 AUDIT_SIGNING_NOT_CONFIGURED when key is not configured."""
    monkeypatch.delenv("HRMS_AUDIT_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("HRMS_AUDIT_KEY_ID", raising=False)

    client = make_client()
    payload = {
        "violation_id": "VIO-001",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-001",
        "patch_payload": {"annual_leave_balance": 20},
    }
    resp = client.post("/v1/remediate/execute", json=payload, headers=bearer(roles=["hr_manager"]))
    assert resp.status_code == 503
    assert resp.json()["error"] == "AUDIT_SIGNING_NOT_CONFIGURED"
