"""Live end-to-end integration QA tests against a real Frappe HRMS system in Docker.

Proves:
- Statutory EPF/EPS calculation against real submitted Salary Slips over HTTP
- Hand-computed rupee values, ceiling boundaries, strict input checks
- Schema fingerprint first-sight / quarantine / approve flow
- Full write-back lifecycle: propose -> approve -> verify in Frappe -> rollback -> verify in Frappe
- Same-person approve rejection, non-writable field rejection, optimistic conflict detection
- Declarative requisition sync: canonical status mapping, unmapped value rejection, multi-page offset paging
- Negative auth, role, and unreachable host paths
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator

import httpx
import pytest
import yaml

from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.server.app import _storage
from tests.integration.frappe_seed import CA_BUNDLE_PATH, seed_frappe

pytestmark = pytest.mark.skipif(
    os.environ.get("HRMS_FRAPPE_SANDBOX") != "1",
    reason="Live Frappe tests require HRMS_FRAPPE_SANDBOX=1",
)


@pytest.fixture(scope="module")
def seeded_data() -> Dict[str, Any]:
    return seed_frappe()


@pytest.fixture(scope="module")
def live_env(seeded_data: Dict[str, Any]) -> Generator[Dict[str, str], None, None]:
    saved_env = dict(os.environ)  # restored after the module so nothing leaks into other tests
    # 1. SSL CA bundle
    os.environ["SSL_CERT_FILE"] = CA_BUNDLE_PATH

    # 2. Host credentials
    os.environ["FRAPPE_API_KEY"] = seeded_data["api_key"]
    os.environ["FRAPPE_API_SECRET"] = seeded_data["api_secret"]
    os.environ["WRONG_API_KEY"] = "bad_key_value"
    os.environ["WRONG_API_SECRET"] = "bad_secret_value"

    # 3. Host registry
    hosts_config = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://localhost:8443",
                "api_key_env": "FRAPPE_API_KEY",
                "api_secret_env": "FRAPPE_API_SECRET",
                "timeout_seconds": 30,
                "writable_fields": {
                    "Employee": {
                        "notice_number_of_days": "int",
                    }
                },
            },
            {
                "tenant_id": "globex",
                "vendor": "frappe",
                "base_url": "https://localhost:8443",
                "api_key_env": "WRONG_API_KEY",
                "api_secret_env": "WRONG_API_SECRET",
                "timeout_seconds": 30,
                "writable_fields": {},
            },
        ]
    }
    hosts_file = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(hosts_config, hosts_file)
    hosts_file.close()
    os.environ["HRMS_HOSTS"] = hosts_file.name

    # 4. Payroll rules (# SANDBOX TEST DATA)
    rules_config = {
        "tenants": [
            {
                "tenant_id": "acme",
                "rules_version": "2026-10-sandbox-1",
                "currency": "INR",
                "pf": {
                    "required_components": ["B"],
                    "optional_components": ["DA"],
                    "employee_rate": "0.12",
                    "employer_rate": "0.12",
                    "eps_rate": "0.0833",
                    "wage_ceiling": "15000",
                    "eps_wage_ceiling": "15000",
                    "apply_ceiling_to_employee": True,
                    "apply_ceiling_to_employer": True,
                },
            },
            {
                "tenant_id": "globex",
                "rules_version": "2026-10-sandbox-1",
                "currency": "INR",
                "pf": {
                    "required_components": ["B"],
                    "optional_components": ["DA"],
                    "employee_rate": "0.12",
                    "employer_rate": "0.12",
                    "eps_rate": "0.0833",
                    "wage_ceiling": "15000",
                    "eps_wage_ceiling": "15000",
                    "apply_ceiling_to_employee": True,
                    "apply_ceiling_to_employer": True,
                },
            },
        ]
    }
    rules_file = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(rules_config, rules_file)
    rules_file.close()
    os.environ["HRMS_PAYROLL_RULES"] = rules_file.name

    # 5. Pre-approve schema fingerprint for Salary Slip so standard calculation passes
    headers = {"Authorization": f"token {seeded_data['api_key']}:{seeded_data['api_secret']}"}
    with httpx.Client() as http_client:
        r = http_client.get(
            f"https://localhost:8443/api/resource/Salary Slip/{seeded_data['slip_below_ceiling']}",
            headers=headers,
        )
        r.raise_for_status()
        raw_slip = r.json()["data"]
        fp = slip_fingerprint(raw_slip)

    _storage.set(
        "approved_fingerprints",
        "acme:frappe:Salary Slip",
        {"fingerprint": fp, "approved_by": "bootstrap", "approved_at": 0.0},
    )

    yield {
        "hosts_path": hosts_file.name,
        "rules_path": rules_file.name,
        "approved_fp": fp,
    }

    Path(hosts_file.name).unlink(missing_ok=True)
    Path(rules_file.name).unlink(missing_ok=True)
    os.environ.clear()
    os.environ.update(saved_env)


def _frappe_get_employee(seeded: Dict[str, Any], emp_id: str) -> Dict[str, Any]:
    headers = {"Authorization": f"token {seeded['api_key']}:{seeded['api_secret']}"}
    with httpx.Client() as client:
        resp = client.get(f"https://localhost:8443/api/resource/Employee/{emp_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()["data"]


def _frappe_update_employee(seeded: Dict[str, Any], emp_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"Authorization": f"token {seeded['api_key']}:{seeded['api_secret']}"}
    with httpx.Client() as client:
        resp = client.put(f"https://localhost:8443/api/resource/Employee/{emp_id}", json=patch, headers=headers)
        resp.raise_for_status()
        return resp.json()["data"]


# ===================================================================================================
# 1. PAYROLL PF CALCULATION (POSITIVE, BOUNDARY, NEGATIVE)
# ===================================================================================================


def test_pf_calculate_below_ceiling_exact_values(make_client, bearer, live_env, seeded_data):
    """Employee 1 below wage ceiling (Basic=10000, DA=2000 -> PF Wage=12000).

    Statutory EPF test values:
    - pf_wage = 12000
    - employee = round(12000 * 0.12) = 1440
    - employer_total = round(12000 * 0.12) = 1440
    - employer_eps = round(12000 * 0.0833) = round(999.6) = 1000
    - employer_epf = 1440 - 1000 = 440
    """
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["employee_id"] == seeded_data["employee_below_ceiling"]
    assert data["currency"] == "INR"
    assert data["pf_wage"] == "12000.0" or data["pf_wage"] == "12000"
    assert data["employee_contribution"] == "1440"
    assert data["employer_total"] == "1440"
    assert data["employer_eps"] == "1000"
    assert data["employer_epf"] == "440"

    prov = data["provenance"]
    assert prov["vendor"] == "frappe"
    assert prov["doctype"] == "Salary Slip"
    assert prov["document"] == seeded_data["slip_below_ceiling"]
    assert prov["rules_version"] == "2026-10-sandbox-1"
    assert prov["fingerprint"].startswith("sha256:")
    assert len(prov["components"]) == 2
    abbrs = {c["abbr"]: c["amount"] for c in prov["components"]}
    assert abbrs == {"B": "10000.0", "DA": "2000.0"}


def test_pf_calculate_above_ceiling_exact_values(make_client, bearer, live_env, seeded_data):
    """Employee 2 above wage ceiling (Basic=20000, DA=5000 -> PF Wage=25000).

    Statutory EPF test values with 15000 ceiling:
    - pf_wage = 25000
    - employee_base = min(25000, 15000) = 15000 -> round(15000 * 0.12) = 1800
    - employer_base = min(25000, 15000) = 15000 -> round(15000 * 0.12) = 1800
    - employer_eps = round(min(25000, 15000) * 0.0833) = round(1249.5) = 1250 (ROUND_HALF_UP)
    - employer_epf = 1800 - 1250 = 550
    """
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_above_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["employee_id"] == seeded_data["employee_above_ceiling"]
    assert data["currency"] == "INR"
    assert data["pf_wage"] == "25000.0" or data["pf_wage"] == "25000"
    assert data["employee_contribution"] == "1800"
    assert data["employer_total"] == "1800"
    assert data["employer_eps"] == "1250"
    assert data["employer_epf"] == "550"


def test_pf_calculate_schema_fingerprint_flow(make_client, bearer, live_env, seeded_data):
    """Schema fingerprint flow: unapproved -> 409 + fingerprint -> approve -> 200 -> drift -> quarantine."""
    client = make_client()

    # 1. Unapproved state
    _storage.delete("approved_fingerprints", "acme:frappe:Salary Slip")
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "MAPPING_NOT_APPROVED"
    observed = body["observed_fingerprint"]
    assert observed.startswith("sha256:")

    # 2. Approve observed fingerprint
    client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": observed},
        headers=bearer(tenant_id="acme", sub="admin_user_1", roles=["platform_admin"]),
    )
    appr_resp = client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": observed},
        headers=bearer(tenant_id="acme", sub="admin_user_2", roles=["platform_admin"]),
    )
    assert appr_resp.status_code == 200
    assert appr_resp.json()["fingerprint"] == observed

    # 3. Calculation now succeeds
    ok_resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert ok_resp.status_code == 200

    # 4. Schema drift: store has a different approved fingerprint
    tampered_fp = "sha256:" + "0" * 64
    client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": tampered_fp},
        headers=bearer(tenant_id="acme", sub="admin_user_1", roles=["platform_admin"]),
    )
    client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": tampered_fp},
        headers=bearer(tenant_id="acme", sub="admin_user_2", roles=["platform_admin"]),
    )
    drift_resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert drift_resp.status_code == 409
    assert drift_resp.json()["error"] == "SCHEMA_DRIFT"

    # 5. Quarantine inspection
    q_resp = client.get(
        "/v1/schema/fingerprints/quarantine",
        headers=bearer(tenant_id="acme", roles=["platform_admin"]),
    )
    assert q_resp.status_code == 200
    quarantine = q_resp.json()["quarantine"]
    assert any(q.get("doctype") == "Salary Slip" and q.get("expected_fingerprint") == tampered_fp for q in quarantine)

    # Restore correct approval for subsequent tests
    client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": observed},
        headers=bearer(tenant_id="acme", sub="admin_user_1", roles=["platform_admin"]),
    )
    client.post(
        "/v1/schema/fingerprints/approve",
        json={"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": observed},
        headers=bearer(tenant_id="acme", sub="admin_user_2", roles=["platform_admin"]),
    )


def test_pf_calculate_strict_mode(make_client, bearer, live_env, seeded_data):
    """Extra fields in PfCalculateRequest are rejected by strict validation (extra='forbid')."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10", "injected_wage": 50000},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 422


def test_pf_calculate_unknown_employee(make_client, bearer, live_env):
    """Non-existent employee returns 404 SLIP_NOT_FOUND."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "UNKNOWN_EMP_99999", "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SLIP_NOT_FOUND"


def test_pf_calculate_unknown_period(make_client, bearer, live_env, seeded_data):
    """Period with no submitted salary slip returns 404 SLIP_NOT_FOUND."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2025-01"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SLIP_NOT_FOUND"


def test_pf_calculate_draft_slip_rejected(make_client, bearer, live_env, seeded_data):
    """Draft slip (docstatus=0) is not returned by fetch_submitted_salary_slip -> 404 SLIP_NOT_FOUND."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-11"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SLIP_NOT_FOUND"


def test_pf_calculate_cancelled_slip_rejected(make_client, bearer, live_env, seeded_data):
    """Cancelled slip (docstatus=2) is not returned -> 404 SLIP_NOT_FOUND."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-09"},
        headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SLIP_NOT_FOUND"


def test_pf_calculate_wrong_credentials(make_client, bearer, live_env, seeded_data):
    """Host with invalid credentials returns 502 HOST_ERROR."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant="globex", roles=["hr_manager"]),
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "HOST_ERROR"


def test_pf_calculate_unreachable_host(make_client, bearer, live_env, seeded_data):
    """Host on unreachable port returns 502 HOST_ERROR without altering containers."""
    unreachable_config = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://localhost:8444",  # wrong port
                "api_key_env": "FRAPPE_API_KEY",
                "api_secret_env": "FRAPPE_API_SECRET",
                "timeout_seconds": 3,
                "writable_fields": {},
            }
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(unreachable_config, f)
        temp_hosts = f.name

    orig_hosts = os.environ.get("HRMS_HOSTS")
    os.environ["HRMS_HOSTS"] = temp_hosts
    try:
        client = make_client()
        resp = client.post(
            "/v1/payroll/pf/calculate",
            json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
            headers=bearer(tenant_id="acme", roles=["payroll_officer"]),
        )
        assert resp.status_code == 502
        assert resp.json()["error"] == "HOST_ERROR"
    finally:
        if orig_hosts:
            os.environ["HRMS_HOSTS"] = orig_hosts
        Path(temp_hosts).unlink(missing_ok=True)


def test_pf_calculate_auth_negatives(make_client, bearer, live_env, seeded_data):
    """Auth negatives: missing token -> 401, employee role (lacking VIEW_SALARY) -> 403."""
    client = make_client()
    # No auth
    no_auth = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
    )
    assert no_auth.status_code == 401

    # Forbidden role
    forbidden = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": seeded_data["employee_below_ceiling"], "period": "2026-10"},
        headers=bearer(tenant_id="acme", roles=["staff"]),
    )
    assert forbidden.status_code == 403


# ===================================================================================================
# 2. WRITE-BACK (POSITIVE, ROBUSTNESS, INTEGRITY, CONFLICT)
# ===================================================================================================


def test_writeback_propose_approve_verify_rollback_cycle(make_client, bearer, live_env, seeded_data):
    """Full lifecycle: propose -> approve by different user -> verify in Frappe -> rollback -> verify in Frappe."""
    emp_id = seeded_data["employee_below_ceiling"]
    # Ensure starting state
    _frappe_update_employee(seeded_data, emp_id, {"notice_number_of_days": 30})

    client = make_client()

    # 1. Propose change from 30 to 60
    prop_resp = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": emp_id, "patch": {"notice_number_of_days": 60}},
        headers=bearer(tenant_id="acme", sub="proposer_1", roles=["hr_manager"]),
    )
    assert prop_resp.status_code == 200, prop_resp.text
    prop_data = prop_resp.json()
    proposal_id = prop_data["id"]
    assert prop_data["status"] == "PENDING"
    assert prop_data["patch"] == {"notice_number_of_days": 60}
    assert prop_data["before"] == {"notice_number_of_days": 30}

    # 2. Approve by a different user
    appr_resp = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant_id="acme", sub="approver_2", roles=["hr_manager"]),
    )
    assert appr_resp.status_code == 200, appr_resp.text
    assert appr_resp.json()["status"] == "EXECUTED"

    # 3. Verify in Frappe directly via REST
    emp_frappe = _frappe_get_employee(seeded_data, emp_id)
    assert emp_frappe["notice_number_of_days"] == 60

    # 4. Rollback
    rb_req = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant_id="acme", sub="approver_2", roles=["hr_manager"]),
    )
    assert rb_req.status_code == 202, rb_req.text
    rb_resp = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant_id="acme", sub="approver_3", roles=["hr_manager"]),
    )
    assert rb_resp.status_code == 200, rb_resp.text
    assert rb_resp.json()["status"] == "ROLLED_BACK"

    # 5. Verify restored in Frappe directly via REST
    emp_restored = _frappe_get_employee(seeded_data, emp_id)
    assert emp_restored["notice_number_of_days"] == 30


def test_writeback_same_person_approve_rejected(make_client, bearer, live_env, seeded_data):
    """Separation of duties: approver cannot be the proposer -> 403 SAME_PERSON."""
    emp_id = seeded_data["employee_below_ceiling"]
    client = make_client()

    prop_resp = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": emp_id, "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant_id="acme", sub="sole_user", roles=["hr_manager"]),
    )
    assert prop_resp.status_code == 200
    prop_id = prop_resp.json()["id"]

    # Self-approval attempt
    appr_resp = client.post(
        f"/v1/writeback/proposals/{prop_id}/approve",
        headers=bearer(tenant_id="acme", sub="sole_user", roles=["hr_manager"]),
    )
    assert appr_resp.status_code == 403
    assert appr_resp.json()["error"] == "SAME_PERSON"


def test_writeback_non_writable_field_rejected(make_client, bearer, live_env, seeded_data):
    """Fields not on writable_fields allowlist are rejected -> 422 FIELD_NOT_WRITABLE."""
    emp_id = seeded_data["employee_below_ceiling"]
    client = make_client()

    resp = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": emp_id, "patch": {"designation": "Manager"}},
        headers=bearer(tenant_id="acme", sub="proposer_1", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "FIELD_NOT_WRITABLE"


def test_writeback_optimistic_conflict_detected(make_client, bearer, live_env, seeded_data):
    """Direct modification in Frappe between propose and approve results in 409 CONFLICT; Frappe is untouched."""
    emp_id = seeded_data["employee_below_ceiling"]
    _frappe_update_employee(seeded_data, emp_id, {"notice_number_of_days": 30})

    client = make_client()

    # 1. Propose notice_number_of_days: 75
    prop_resp = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": emp_id, "patch": {"notice_number_of_days": 75}},
        headers=bearer(tenant_id="acme", sub="proposer_1", roles=["hr_manager"]),
    )
    assert prop_resp.status_code == 200
    prop_id = prop_resp.json()["id"]

    # 2. Modify directly in Frappe out-of-band to change modified timestamp
    _frappe_update_employee(seeded_data, emp_id, {"notice_number_of_days": 35})

    # 3. Approve should detect modified mismatch -> 409 CONFLICT
    appr_resp = client.post(
        f"/v1/writeback/proposals/{prop_id}/approve",
        headers=bearer(tenant_id="acme", sub="approver_2", roles=["hr_manager"]),
    )
    assert appr_resp.status_code == 409
    assert appr_resp.json()["error"] == "CONFLICT"

    # 4. Verify Frappe was NOT updated to 75
    emp_current = _frappe_get_employee(seeded_data, emp_id)
    assert emp_current["notice_number_of_days"] == 35

    # Reset
    _frappe_update_employee(seeded_data, emp_id, {"notice_number_of_days": 30})


# ===================================================================================================
# 3. REQUISITIONS (RECORD INTEGRITY, STATUS MAPPING, REJECTION, PAGING, AUTH)
# ===================================================================================================


def test_requisitions_records_and_status_mapping(make_client, bearer, live_env, seeded_data):
    """GET /v1/hosts/requisitions returns canonical records matching Frappe data and correctly maps statuses."""
    client = make_client()
    resp = client.get(
        "/v1/hosts/requisitions",
        headers=bearer(tenant_id="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["vendor"] == "frappe"
    reqs = data["requisitions"]
    assert len(reqs) > 0

    # Verify canonical field presence
    sample = reqs[0]
    for key in ("id", "title", "department", "headcount", "status", "description", "hiring_manager_ref"):
        assert key in sample

    # Check status mapping: Pending -> PENDING, Open & Approved -> OPEN
    statuses = {r["status"] for r in reqs}
    assert "PENDING" in statuses
    assert "OPEN" in statuses


def test_requisitions_unmapped_status_reported_as_rejected(make_client, bearer, live_env, seeded_data):
    """Job Requisition with unmapped status 'Archived' is rejected as UNMAPPED_VALUE."""
    client = make_client()
    resp = client.get(
        "/v1/hosts/requisitions",
        headers=bearer(tenant_id="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200
    data = resp.json()

    rejected = data["rejected"]
    unmapped_match = [r for r in rejected if r.get("id") == seeded_data["unmapped_requisition_id"]]
    assert len(unmapped_match) == 1
    assert unmapped_match[0]["error"] == "UNMAPPED_VALUE"
    assert unmapped_match[0]["field"] == "status"


def test_requisitions_multipage_offset_paging(make_client, bearer, live_env, seeded_data):
    """105 seeded Job Requisitions with page size 100 triggers multi-page offset paging (fetches page 0 and 1)."""
    client = make_client()
    resp = client.get(
        "/v1/hosts/requisitions",
        headers=bearer(tenant_id="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200
    data = resp.json()

    # Total records returned (accepted + rejected) equals total seeded in Frappe
    total_returned = len(data["requisitions"]) + len(data["rejected"])
    assert total_returned == seeded_data["job_requisitions_count"]
    assert total_returned >= 105


def test_requisitions_auth_and_role_negatives(make_client, bearer, live_env):
    """Requisition endpoint auth negatives: 401 without auth, 403 without MANAGE_RECRUITMENT."""
    client = make_client()

    # Missing token
    resp_no_auth = client.get("/v1/hosts/requisitions")
    assert resp_no_auth.status_code == 401

    # Forbidden role (staff lacks MANAGE_RECRUITMENT)
    resp_forbidden = client.get(
        "/v1/hosts/requisitions",
        headers=bearer(tenant_id="acme", roles=["staff"]),
    )
    assert resp_forbidden.status_code == 403
