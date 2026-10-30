"""Approved, idempotent write-back with rollback (piece F2: W1-W11)."""

import asyncio
import copy

import httpx
import pytest
import yaml

from hrms_plugin.integrations.frappe import FrappeClient
from hrms_plugin.integrations.host_registry import HostRegistryError, load_host_registry
from hrms_plugin.security.pii import identifier_kind
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from hrms_plugin.writeback.service import WritebackError, WritebackService

HOSTS = {
    "tenants": [
        {
            "tenant_id": "acme",
            "vendor": "frappe",
            "base_url": "https://hr.acme.test",
            "api_key_env": "ACME_FRAPPE_KEY",
            "api_secret_env": "ACME_FRAPPE_SECRET",
            "timeout_seconds": 10,
            "writable_fields": {"Employee": {"notice_number_of_days": "int", "final_confirmation_date": "date"}},
        }
    ]
}
PROPOSER = AuthContext(user_id="hr-1", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
APPROVER = AuthContext(user_id="hr-2", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))


class StatefulFrappe:
    def __init__(self):
        self.doc = {
            "name": "HR-EMP-00001",
            "employee_name": "Sunita Verma",
            "notice_number_of_days": 30,
            "final_confirmation_date": "2026-03-01",
            "modified": "2026-10-01 10:00:00.000001",
        }
        self.version = 1
        self.puts = []
        self.fail_put = False
        self.delay = 0.0

    def touch(self):
        self.version += 1
        self.doc["modified"] = f"2026-10-01 10:00:00.{self.version:06d}"

    async def handler(self, request):
        if self.delay:
            await asyncio.sleep(self.delay)  # lets concurrent requests genuinely interleave
        if request.url.path != "/api/resource/Employee/HR-EMP-00001":
            return httpx.Response(404, json={})
        if request.method == "GET":
            return httpx.Response(200, json={"data": dict(self.doc)})
        if request.method == "PUT":
            if self.fail_put:
                return httpx.Response(500, json={"exc": "boom"})
            body = __import__("json").loads(request.content)
            self.puts.append({"body": body, "headers": dict(request.headers)})
            self.doc.update(body)
            self.touch()
            return httpx.Response(200, json={"data": dict(self.doc)})
        return httpx.Response(405, json={})


@pytest.fixture
def frappe():
    return StatefulFrappe()


@pytest.fixture
def svc(tmp_path, monkeypatch, frappe):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    clock = {"now": 1_800_000_000.0}

    def factory(host, key, secret):
        return FrappeClient(
            host, key, secret, http_client=httpx.AsyncClient(transport=httpx.MockTransport(frappe.handler))
        )

    service = WritebackService(store=MemoryKeyValueStore(), client_factory=factory, clock=lambda: clock["now"])
    service.test_clock = clock
    return service


async def codes(coro):
    with pytest.raises(WritebackError) as info:
        await coro
    return info.value.code


# W1/W4 propose -------------------------------------------------------------------------------------


async def test_propose_returns_diff_and_stores_pending(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    assert p["status"] == "PENDING" and p["proposed_by"] == "hr-1"
    assert p["diff"] == [{"field": "notice_number_of_days", "before": 30, "after": 60}]
    assert frappe.puts == []


@pytest.mark.parametrize(
    "patch,code",
    [
        ({}, "INVALID_VALUE"),
        ({"employee_name": "X"}, "FIELD_NOT_WRITABLE"),
        ({"notice_number_of_days": True}, "INVALID_VALUE"),
        ({"notice_number_of_days": "60"}, "INVALID_VALUE"),
        ({"final_confirmation_date": "2026-02-30"}, "INVALID_VALUE"),
        ({"final_confirmation_date": "01-03-2026"}, "INVALID_VALUE"),
        ({"notice_number_of_days": 30}, "NO_CHANGE"),
    ],
)
async def test_propose_rejections(svc, patch, code):
    assert await codes(svc.propose(PROPOSER, "Employee", "HR-EMP-00001", patch)) == code


async def test_doctype_not_in_allowlist(svc):
    assert await codes(svc.propose(PROPOSER, "Salary Slip", "X", {"a": 1})) == "FIELD_NOT_WRITABLE"


async def test_leap_day_is_a_valid_date(svc):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"final_confirmation_date": "2028-02-29"})
    assert p["status"] == "PENDING"


# W2/W5/W6/W8 approve ---------------------------------------------------------------------------------


async def test_approve_writes_once_with_audit_headers(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    done = await svc.approve(APPROVER, p["id"])
    assert done["status"] == "EXECUTED" and frappe.doc["notice_number_of_days"] == 60
    assert frappe.puts[0]["body"] == {"notice_number_of_days": 60}
    assert frappe.puts[0]["headers"]["x-hitl-approver-id"] == "hr-2"
    assert frappe.puts[0]["headers"]["authorization"] == "token k:s"
    again = await svc.approve(APPROVER, p["id"])
    assert again == done and len(frappe.puts) == 1  # idempotent


async def test_proposer_cannot_approve(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    assert await codes(svc.approve(PROPOSER, p["id"])) == "SAME_PERSON"
    assert frappe.puts == []


async def test_concurrent_edit_since_proposal_is_a_conflict(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    frappe.doc["notice_number_of_days"] = 45
    frappe.touch()  # someone edited in Frappe after the proposal
    assert await codes(svc.approve(APPROVER, p["id"])) == "CONFLICT"
    assert frappe.puts == [] and (await svc.get(APPROVER, p["id"]))["status"] == "CONFLICT"


async def test_double_click_writes_at_most_once(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    frappe.delay = 0.02
    results = await asyncio.gather(svc.approve(APPROVER, p["id"]), svc.approve(APPROVER, p["id"]))
    assert len(frappe.puts) == 1 and results[0] == results[1]


async def test_expiry_boundary(svc):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    svc.test_clock["now"] += 24 * 3600
    assert (await svc.approve(APPROVER, p["id"]))["status"] == "EXECUTED"  # exactly 24 h: still valid
    q = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 90})
    svc.test_clock["now"] += 24 * 3600 + 1
    assert await codes(svc.approve(APPROVER, q["id"])) == "EXPIRED"


async def test_host_failure_on_put_leaves_proposal_pending(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    frappe.fail_put = True
    assert await codes(svc.approve(APPROVER, p["id"])) == "HOST_ERROR"
    assert (await svc.get(APPROVER, p["id"]))["status"] == "PENDING"


async def test_other_tenant_cannot_see_or_approve(svc):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    other = AuthContext(user_id="x", tenant_id="globex", roles=frozenset({UserRole.HR_ADMIN}))
    assert await codes(svc.approve(other, p["id"])) == "NOT_FOUND"


# W5/W6/W7 rollback ------------------------------------------------------------------------------------


async def test_rollback_restores_before_values_once(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await svc.approve(APPROVER, p["id"])
    req = await svc.rollback(APPROVER, p["id"])
    assert req["status"] == "ROLLBACK_PENDING"
    rolled = await svc.rollback(PROPOSER, p["id"])
    assert rolled["status"] == "ROLLED_BACK" and frappe.doc["notice_number_of_days"] == 30
    assert frappe.puts[-1]["body"] == {"notice_number_of_days": 30}
    assert await svc.rollback(PROPOSER, p["id"]) == rolled and len(frappe.puts) == 2


async def test_rollback_after_someone_else_edited_is_refused(svc, frappe):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await svc.approve(APPROVER, p["id"])
    await svc.rollback(APPROVER, p["id"])
    frappe.doc["notice_number_of_days"] = 75
    frappe.touch()
    assert await codes(svc.rollback(PROPOSER, p["id"])) == "ROLLBACK_CONFLICT"
    assert frappe.doc["notice_number_of_days"] == 75 and len(frappe.puts) == 1


async def test_rollback_of_pending_is_invalid(svc):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    assert await codes(svc.rollback(APPROVER, p["id"])) == "INVALID_STATE"


async def test_approve_after_rollback_is_invalid(svc):
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await svc.approve(APPROVER, p["id"])
    await svc.rollback(APPROVER, p["id"])
    await svc.rollback(PROPOSER, p["id"])
    assert await codes(svc.approve(APPROVER, p["id"])) == "INVALID_STATE"


# W3/W10 config ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["employee_name", "first_name", "cell_number", "company_email", "bank_ac_no", "personal_email"]
)
def test_pii_fields_cannot_be_writable(tmp_path, field):
    data = copy.deepcopy(HOSTS)
    data["tenants"][0]["writable_fields"] = {"Employee": {field: "string"}}
    path = tmp_path / "h.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(HostRegistryError):
        load_host_registry(path)


@pytest.mark.parametrize(
    "field,kind", [("cell_number", "PHONE"), ("company_email", "EMAIL"), ("bank_ac_no", "BANK_ACCOUNT")]
)
def test_frappe_identifier_fields_are_known_pii(field, kind):
    assert identifier_kind(field) == kind


def test_unknown_field_type_rejected(tmp_path):
    data = copy.deepcopy(HOSTS)
    data["tenants"][0]["writable_fields"] = {"Employee": {"notice_number_of_days": "float"}}
    path = tmp_path / "h.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(HostRegistryError):
        load_host_registry(path)


# W2/W11 HTTP ----------------------------------------------------------------------------------------------


def _http_env(monkeypatch, tmp_path, frappe):
    from hrms_plugin.server import app as app_module

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump(HOSTS))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "k")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "s")
    monkeypatch.setattr(
        app_module.writeback_service,
        "_client_factory",
        lambda h, k, s: FrappeClient(
            h, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(frappe.handler))
        ),
    )


def test_http_flow_with_separation_of_duties(make_client, bearer, monkeypatch, tmp_path, frappe):
    _http_env(monkeypatch, tmp_path, frappe)
    client = make_client()
    body = {"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 60}}
    proposed = client.post("/v1/writeback/proposals", json=body, headers=bearer(sub="hr-1"))
    assert proposed.status_code == 200
    pid = proposed.json()["id"]
    same = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=bearer(sub="hr-1"))
    assert same.status_code == 403 and same.json()["error"] == "SAME_PERSON"
    ok = client.post(f"/v1/writeback/proposals/{pid}/approve", headers=bearer(sub="hr-2"))
    assert ok.status_code == 200 and ok.json()["status"] == "EXECUTED"
    rb_req = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="hr-2"))
    assert rb_req.status_code == 202 and rb_req.json()["status"] == "ROLLBACK_PENDING"
    back = client.post(f"/v1/writeback/proposals/{pid}/rollback", headers=bearer(sub="hr-3"))
    assert back.status_code == 200 and back.json()["status"] == "ROLLED_BACK"


def test_http_permissions_and_freshness(make_client, bearer, monkeypatch, tmp_path, frappe):
    from tests.support.auth import NOW

    _http_env(monkeypatch, tmp_path, frappe)
    client = make_client()
    body = {"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 60}}
    assert (
        client.post("/v1/writeback/proposals", json=body, headers=bearer(roles=["payroll_officer"])).status_code == 403
    )
    pid = client.post("/v1/writeback/proposals", json=body, headers=bearer(sub="hr-1")).json()["id"]
    stale = bearer(sub="hr-2", iat=NOW - 301)
    assert client.post(f"/v1/writeback/proposals/{pid}/approve", headers=stale).status_code == 401
    assert client.post("/v1/writeback/proposals/nope/approve", headers=bearer(sub="hr-2")).status_code == 404


def test_http_bad_body_rejected(make_client, bearer):
    res = make_client().post(
        "/v1/writeback/proposals",
        headers=bearer(),
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {}, "x": 1},
    )
    assert res.status_code == 422


# agy F2-review fixes -----------------------------------------------------------------------------------------

async def test_rollback_carries_audit_headers_and_records_audit(svc, frappe):          # F1 / F6
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await svc.approve(APPROVER, p["id"])
    await svc.rollback(PROPOSER, p["id"])
    rolled = await svc.rollback(APPROVER, p["id"])
    headers = frappe.puts[-1]["headers"]
    assert headers["x-hitl-approver-id"] == "hr-2" and headers["x-agent-name"] == "WritebackService"
    assert headers["x-agent-invocation-id"] != frappe.puts[0]["headers"]["x-agent-invocation-id"]
    assert rolled["rollback_audit"]["X-HITL-Approver-Id"] == "hr-2"


def test_middle_name_is_pii_and_cannot_be_writable(tmp_path):                            # F3
    assert identifier_kind("middle_name") == "NAME"
    data = copy.deepcopy(HOSTS)
    data["tenants"][0]["writable_fields"] = {"Employee": {"middle_name": "string"}}
    path = tmp_path / "h.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(HostRegistryError):
        load_host_registry(path)


async def test_missing_modified_after_write_is_recovered_by_reread(svc, frappe):          # F4
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    real = frappe.handler

    async def no_modified_on_put(request):
        response = await real(request)
        if request.method == "PUT":
            data = __import__("json").loads(response.content)["data"]
            data.pop("modified")
            return httpx.Response(200, json={"data": data})
        return response

    frappe.handler = no_modified_on_put
    done = await svc.approve(APPROVER, p["id"])
    assert done["status"] == "EXECUTED" and done["after_modified"] == frappe.doc["modified"]
    frappe.handler = real
    await svc.rollback(APPROVER, p["id"])
    assert (await svc.rollback(PROPOSER, p["id"]))["status"] == "ROLLED_BACK"


async def test_host_error_detail_is_kept(svc, frappe):                                   # F5
    p = await svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    frappe.fail_put = True
    with pytest.raises(WritebackError) as info:
        await svc.approve(APPROVER, p["id"])
    assert info.value.code == "HOST_ERROR" and "500" in info.value.detail


# F2 / F7 — decimal fields ------------------------------------------------------------------------------------

@pytest.fixture
def decimal_svc(svc, frappe, tmp_path, monkeypatch):
    data = copy.deepcopy(HOSTS)
    data["tenants"][0]["writable_fields"]["Employee"]["ctc"] = "decimal"
    path = tmp_path / "hosts_dec.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    frappe.doc["ctc"] = 1500000.5
    return svc


async def test_decimal_equal_to_host_number_is_no_change(decimal_svc):
    assert await codes(decimal_svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"ctc": "1500000.50"})) == "NO_CHANGE"


@pytest.mark.parametrize("value", [1500000.5, "abc", "NaN", "Infinity", True])
async def test_decimal_must_be_finite_decimal_string(decimal_svc, value):
    assert await codes(decimal_svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"ctc": value})) == "INVALID_VALUE"


async def test_decimal_write_and_rollback_restore_exact_values(decimal_svc, frappe):
    p = await decimal_svc.propose(PROPOSER, "Employee", "HR-EMP-00001", {"ctc": "1600000.00"})
    assert p["diff"] == [{"field": "ctc", "before": 1500000.5, "after": "1600000.00"}]
    await decimal_svc.approve(APPROVER, p["id"])
    assert frappe.puts[-1]["body"] == {"ctc": "1600000.00"}
    await decimal_svc.rollback(APPROVER, p["id"])
    await decimal_svc.rollback(PROPOSER, p["id"])
    assert frappe.puts[-1]["body"] == {"ctc": 1500000.5}
