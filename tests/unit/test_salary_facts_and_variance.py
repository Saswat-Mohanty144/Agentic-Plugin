"""QB (Q1/Q2): canonical salary facts with cross-checks, persisted provenance, month-over-month variance review.

The variance threshold is a tenant payroll rule (owner input); without it the check reports NOT_CONFIGURED and never
guesses a threshold.
"""

import copy
from decimal import Decimal

import pytest

from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.payroll.canonical import SalaryFacts, SalaryFactsError, previous_period
from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.payroll.rules import RulesError, load_payroll_rules
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.unit.test_frappe_pf_service import HOSTS_YAML, FrappeMock, service, write
from tests.unit.test_pf_core import RULES_YAML, slip

EMP = "HR-EMP-00001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HRMS_HOSTS", str(write(tmp_path, "hosts.yaml", HOSTS_YAML)))
    monkeypatch.setenv("HRMS_PAYROLL_RULES", str(write(tmp_path, "rules.yaml", RULES_YAML)))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "key123")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "secret456")
    return monkeypatch


def facts(**kw):
    base = dict(period="2026-09", jurisdiction="IN", currency="INR", gross_monthly=Decimal("20000"),
                pf_wage_monthly=Decimal("14000"))
    return SalaryFacts(**{**base, **kw})


# canonical facts --------------------------------------------------------------------------------------------
def test_consistent_facts_are_accepted_including_equal_boundaries():
    f = facts(basic_monthly=Decimal("12000"), dearness_allowance_monthly=Decimal("8000"),
              pf_wage_monthly=Decimal("20000"), annual_ctc=Decimal("240000"))
    assert f.gross_monthly == Decimal("20000")


@pytest.mark.parametrize("kw, reason", [
    ({"gross_monthly": Decimal("-1")}, "negative"),
    ({"pf_wage_monthly": Decimal("NaN")}, "finite"),
    ({"pf_wage_monthly": Decimal("20000.01")}, "pf_wage_monthly exceeds gross_monthly"),
    ({"basic_monthly": Decimal("15000"), "dearness_allowance_monthly": Decimal("5001")},
     "basic_monthly + dearness_allowance_monthly exceeds gross_monthly"),
    ({"annual_ctc": Decimal("239999.99")}, "gross_monthly x 12 exceeds annual_ctc"),
    ({"currency": "AED"}, "currency AED does not match jurisdiction IN"),
    ({"jurisdiction": "AE"}, "currency INR does not match jurisdiction AE"),
    ({"period": "2026-13"}, "period"),
])
def test_inconsistent_facts_are_refused(kw, reason):
    with pytest.raises(SalaryFactsError) as exc:
        facts(**kw)
    assert reason in str(exc.value)


@pytest.mark.parametrize("period, previous", [("2026-09", "2026-08"), ("2026-01", "2025-12"), ("2000-03", "2000-02")])
def test_previous_period(period, previous):
    assert previous_period(period) == previous


# settlement cross-check -------------------------------------------------------------------------------------
def _settle(basic, gross):
    import asyncio

    return asyncio.run(AgenticToolRegistry().execute("compute_final_settlement", {
        "employee_id": "E1", "employee_name": "A", "basic_wage_monthly": basic, "gross_wage_monthly": gross,
        "tenure_years": 3, "jurisdiction": "AE"}))


def test_settlement_refuses_basic_above_gross():
    out = _settle(9001, 9000)
    assert out["error"] == "INVALID_ARGUMENTS" and out["problems"] == ["basic_wage_monthly>gross_wage_monthly"]


def test_settlement_accepts_basic_equal_to_gross():
    assert "error" not in _settle(9000, 9000)


# rules: variance threshold ----------------------------------------------------------------------------------
def _rules(tmp_path, threshold):
    data = copy.deepcopy(RULES_YAML)
    if threshold is not None:
        data["tenants"][0]["pf"]["variance_review_percent"] = threshold
    return write(tmp_path, "rules.yaml", data)


@pytest.mark.parametrize("value", ["0", "-5", "100.01", "abc", 10])
def test_invalid_variance_threshold_rejected(tmp_path, value):
    with pytest.raises(RulesError):
        load_payroll_rules(_rules(tmp_path, value))


@pytest.mark.parametrize("value", ["0.01", "100"])
def test_variance_threshold_boundaries(tmp_path, value):
    assert load_payroll_rules(_rules(tmp_path, value)).tenants[0].pf.variance_review_percent == Decimal(value)


# service: provenance + variance -----------------------------------------------------------------------------
def _slip_for(basic, period):
    import calendar

    year, month = int(period[:4]), int(period[5:])
    last = calendar.monthrange(year, month)[1]
    return {**slip(basic=basic), "start_date": f"{period}-01", "end_date": f"{period}-{last:02d}"}


async def _calc(store, basic, period):
    svc = service(FrappeMock(slips=[_slip_for(basic, period)]), store=store)
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip()), approved_by="admin1")
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip()), approved_by="admin2")
    return svc, await svc.calculate_pf("acme", EMP, period, "IN")


async def test_result_is_persisted_with_provenance(env):
    store = MemoryKeyValueStore()
    svc, result = await _calc(store, 12000, "2026-09")
    stored = svc.get_result("acme", EMP, "2026-09")
    assert stored["result"] == result and stored["result"]["provenance"]["rules_version"] == "2026-10-acme-1"
    assert stored["calculated_at"] > 0 and svc.get_result("globex", EMP, "2026-09") is None


async def test_errors_are_not_persisted(env):
    store = MemoryKeyValueStore()
    svc = service(FrappeMock(slips=[slip()]), store=store)  # fingerprint never approved
    assert (await svc.calculate_pf("acme", EMP, "2026-09", "IN"))["error"] == "MAPPING_NOT_APPROVED"
    assert svc.get_result("acme", EMP, "2026-09") is None


async def test_variance_not_configured_is_reported_not_guessed(env):
    _, result = await _calc(MemoryKeyValueStore(), 12000, "2026-09")
    assert result["variance"] == {"status": "NOT_CONFIGURED"} and result["review_required"] is False


@pytest.mark.parametrize("basic, status, change", [
    (12000, "WITHIN_THRESHOLD", "0.00"),     # 14000 -> 14000
    (13400, "WITHIN_THRESHOLD", "10.00"),    # exactly the threshold is not above it
    (13401, "REVIEW_REQUIRED", "10.01"),
    (15400, "REVIEW_REQUIRED", "24.29"),
    (10000, "REVIEW_REQUIRED", "-14.29"),    # drops count too
])
async def test_month_over_month_variance(env, tmp_path, basic, status, change):
    env.setenv("HRMS_PAYROLL_RULES", str(_rules(tmp_path, "10")))
    store = MemoryKeyValueStore()
    _, first = await _calc(store, 12000, "2026-08")
    assert first["variance"] == {"status": "NO_PREVIOUS_PERIOD"} and first["review_required"] is False
    _, second = await _calc(store, basic, "2026-09")
    assert second["variance"]["status"] == status and second["variance"]["change_percent"] == change
    assert second["variance"]["previous_period"] == "2026-08" and second["variance"]["threshold_percent"] == "10"
    assert second["review_required"] is (status == "REVIEW_REQUIRED")


async def test_currency_must_match_jurisdiction(env, tmp_path):
    data = copy.deepcopy(RULES_YAML)
    data["tenants"][0]["currency"] = "AED"
    env.setenv("HRMS_PAYROLL_RULES", str(write(tmp_path, "rules.yaml", data)))
    svc = service(FrappeMock(slips=[slip(currency="AED")]))
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip(currency="AED")), approved_by="a1")
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip(currency="AED")), approved_by="a2")
    result = await svc.calculate_pf("acme", EMP, "2026-09", "IN")
    assert result["error"] == "INCONSISTENT_PAYLOAD" and "does not match jurisdiction IN" in result["detail"]


# route ------------------------------------------------------------------------------------------------------
def test_stored_result_route(make_client, bearer, monkeypatch):
    svc = service(FrappeMock())
    svc._store.set("pf_results", f"acme:{EMP}:2026-09", {"result": {"pf_wage": "14000"}, "calculated_at": 1.0})
    monkeypatch.setattr(app_module, "payroll_service", svc)
    client = make_client()
    ok = client.get(f"/v1/payroll/pf/results/{EMP}/2026-09", headers=bearer(roles=["payroll_officer"]))
    assert ok.status_code == 200 and ok.json()["result"]["pf_wage"] == "14000"
    assert client.get(f"/v1/payroll/pf/results/{EMP}/2026-08",
                      headers=bearer(roles=["payroll_officer"])).status_code == 404
    assert client.get(f"/v1/payroll/pf/results/{EMP}/2026-09", headers=bearer(roles=["staff"])).status_code == 403
    other = client.get(f"/v1/payroll/pf/results/{EMP}/2026-09",
                       headers=bearer(roles=["hr_manager"], tenant="globex"))
    assert other.status_code in (403, 404)
    assert client.get(f"/v1/payroll/pf/results/{EMP}/2026-9", headers=bearer(roles=["payroll_officer"])).status_code \
        == 422
