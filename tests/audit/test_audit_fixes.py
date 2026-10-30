"""Audit fix tests verifying A3, A4, and A6 requirements."""

from pathlib import Path

import pytest
import yaml

from hrms_plugin.payroll.service import APPROVED_NS, PENDING_NS
from hrms_plugin.server.app import _shape_key, _storage, payroll_service


@pytest.fixture(autouse=True)
def hosts_env(monkeypatch, tmp_path):
    """Fixture setting up HRMS_HOSTS for acme tenant."""
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


def test_no_http_exception_in_server():
    """A4: Assert no HTTPException( remains anywhere in hrms_plugin/server/."""
    server_dir = Path("hrms_plugin/server")
    offenders = []
    for py_file in server_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if "HTTPException(" in content:
            offenders.append(str(py_file))
    assert not offenders, f"Found HTTPException( in server files: {offenders}"


def test_synthesize_non_string_name_returns_422_with_index(make_client, bearer):
    """A3: discovered_fields item with a non-string name returns 422 INVALID_ARGUMENT naming the item index."""
    client = make_client(raise_server_exceptions=False)
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "custom_hr",
            "domain": "REQUISITION",
            "discovered_fields": [
                {"name": "valid_field", "path": "raw.valid"},
                {"name": 12345, "path": "raw.invalid"},
            ],
            "sample_records": [{"raw": {"valid": "ok"}}],
        },
        headers=h_admin,
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("error") == "INVALID_ARGUMENT", f"Expected error INVALID_ARGUMENT, got {data}"
    assert "1" in str(data.get("detail", "")), f"Expected index 1 in detail, got {data}"


def test_synthesize_non_dict_item_returns_422_with_index(make_client, bearer):
    """A3: discovered_fields item that is not a dict returns 422 INVALID_ARGUMENT naming the item index."""
    client = make_client(raise_server_exceptions=False)
    h_admin = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "custom_hr",
            "domain": "REQUISITION",
            "discovered_fields": [
                "not-a-dict",
            ],
            "sample_records": [{"raw": {"valid": "ok"}}],
        },
        headers=h_admin,
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("error") == "INVALID_ARGUMENT", f"Expected error INVALID_ARGUMENT, got {data}"
    assert "0" in str(data.get("detail", "")), f"Expected index 0 in detail, got {data}"


def test_shape_approve_already_approved_and_new_fingerprint(make_client, bearer):
    """A6: Re-approving already-approved shape fingerprint -> 200 ALREADY_APPROVED with no pending record.

    A different fingerprint still returns 202 PENDING_CONFIRMATION and then executes with a second person.
    """
    client = make_client()
    h_admin1 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    h_admin2 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-2")
    fp1 = "sha256:" + "1" * 64
    fp2 = "sha256:" + "2" * 64
    key = _shape_key("acme", "frappe")

    # 1. Observe fp1
    _storage.set(
        "observed_shapes",
        key,
        {"fingerprint": fp1, "shape": {"title": {"present": True, "types": ["str"]}}},
    )

    # 2. First person proposes fp1 -> 202
    r1 = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp1}, headers=h_admin1)
    assert r1.status_code == 202
    assert r1.json()["status"] == "PENDING_CONFIRMATION"

    # 3. Second person approves fp1 -> 200 APPROVED
    r2 = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp1}, headers=h_admin2)
    assert r2.status_code == 200
    assert r2.json()["status"] == "APPROVED"
    assert _storage.get("pending_shapes", key) is None

    # 4. Re-approve fp1 (already approved) -> 200 ALREADY_APPROVED and no pending record created
    r_reapprove = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp1}, headers=h_admin1)
    assert r_reapprove.status_code == 200
    assert r_reapprove.json()["status"] == "ALREADY_APPROVED"
    assert r_reapprove.json()["fingerprint"] == fp1
    assert _storage.get("pending_shapes", key) is None

    # 5. Observe different fingerprint fp2
    _storage.set(
        "observed_shapes",
        key,
        {"fingerprint": fp2, "shape": {"title": {"present": True, "types": ["str"]}}},
    )

    # 6. Propose fp2 -> 202 PENDING_CONFIRMATION
    r_diff1 = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp2}, headers=h_admin1)
    assert r_diff1.status_code == 202
    assert r_diff1.json()["status"] == "PENDING_CONFIRMATION"
    assert _storage.get("pending_shapes", key) is not None

    # 7. Second person approves fp2 -> 200 APPROVED
    r_diff2 = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp2}, headers=h_admin2)
    assert r_diff2.status_code == 200
    assert r_diff2.json()["status"] == "APPROVED"
    assert _storage.get("pending_shapes", key) is None


def test_payroll_fingerprint_approve_already_approved_and_new_fingerprint(make_client, bearer):
    """A6: Re-approving already-approved payroll fingerprint -> 200 ALREADY_APPROVED with no pending record.

    A different fingerprint still returns 202 PENDING_CONFIRMATION and then executes with a second person.
    """
    client = make_client()
    h_admin1 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-1")
    h_admin2 = bearer(tenant="acme", roles=["platform_admin"], sub="admin-2")
    fp1 = "sha256:" + "3" * 64
    fp2 = "sha256:" + "4" * 64
    key = "acme:frappe:Salary Slip"

    # 1. First person proposes fp1 -> 202
    r1 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp1},
        headers=h_admin1,
    )
    assert r1.status_code == 202
    assert r1.json()["status"] == "PENDING_CONFIRMATION"

    # 2. Second person approves fp1 -> 200 APPROVED
    r2 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp1},
        headers=h_admin2,
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "APPROVED"
    assert payroll_service._store.get(PENDING_NS, key) is None
    assert payroll_service._store.get(APPROVED_NS, key) is not None

    # 3. Re-approve fp1 (already approved) -> 200 ALREADY_APPROVED and no pending record
    r_reapprove = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp1},
        headers=h_admin1,
    )
    assert r_reapprove.status_code == 200
    assert r_reapprove.json()["status"] == "ALREADY_APPROVED"
    assert r_reapprove.json()["fingerprint"] == fp1
    assert payroll_service._store.get(PENDING_NS, key) is None

    # 4. Propose different fingerprint fp2 -> 202 PENDING_CONFIRMATION
    r_diff1 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp2},
        headers=h_admin1,
    )
    assert r_diff1.status_code == 202
    assert r_diff1.json()["status"] == "PENDING_CONFIRMATION"
    assert payroll_service._store.get(PENDING_NS, key) is not None

    # 5. Second person approves fp2 -> 200 APPROVED
    r_diff2 = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": fp2},
        headers=h_admin2,
    )
    assert r_diff2.status_code == 200
    assert r_diff2.json()["status"] == "APPROVED"
    assert payroll_service._store.get(PENDING_NS, key) is None
