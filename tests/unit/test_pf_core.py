"""Payroll rules, slip fingerprint, validation and PF computation (piece F1 §2: F1-1, F1-4, F1-6, F1-8, F1-9)."""

import copy
from decimal import Decimal

import pytest
import yaml

from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.payroll.pf import PfError, compute_pf
from hrms_plugin.payroll.rules import RulesError, load_payroll_rules

RULES_YAML = {
    "tenants": [
        {
            "tenant_id": "acme",
            "rules_version": "2026-10-acme-1",
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
        }
    ]
}


def rules_file(tmp_path, data=None):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data or RULES_YAML))
    return path


@pytest.fixture
def rules(tmp_path):
    return load_payroll_rules(rules_file(tmp_path)).for_tenant("acme")


def slip(basic=12000, da=2000, hra=6000, special=None, gross=None, currency="INR", extra_earnings=()):
    earnings = [
        {
            "salary_component": "Basic",
            "abbr": "B",
            "amount": basic,
            "statistical_component": 0,
            "do_not_include_in_total": 0,
        },
        {
            "salary_component": "Dearness Allowance",
            "abbr": "DA",
            "amount": da,
            "statistical_component": 0,
            "do_not_include_in_total": 0,
        },
        {
            "salary_component": "House Rent Allowance",
            "abbr": "HRA",
            "amount": hra,
            "statistical_component": 0,
            "do_not_include_in_total": 0,
        },
        *extra_earnings,
    ]
    if special is not None:
        earnings.append(
            {
                "salary_component": "Special",
                "abbr": "SA",
                "amount": special,
                "statistical_component": 0,
                "do_not_include_in_total": 0,
            }
        )
    counted = sum(e["amount"] for e in earnings if not e["statistical_component"] and not e["do_not_include_in_total"])
    return {
        "name": "Sal Slip/HR-EMP-00001/00007",
        "employee": "HR-EMP-00001",
        "employee_name": "Sunita Verma",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "currency": currency,
        "gross_pay": counted if gross is None else gross,
        "docstatus": 1,
        "modified": "2026-10-01 10:00:00",
        "earnings": earnings,
    }


# Rules ----------------------------------------------------------------------------------------------------


def test_rules_load_with_exact_decimals(rules):
    assert rules.pf.employee_rate == Decimal("0.12") and rules.pf.wage_ceiling == Decimal("15000")
    assert rules.pf.required_components == ("B",) and rules.rules_version == "2026-10-acme-1"


def test_unknown_tenant_has_no_rules(tmp_path):
    assert load_payroll_rules(rules_file(tmp_path)).for_tenant("globex") is None


@pytest.mark.parametrize(
    "path,value",
    [
        ("required_components", []),
        ("employee_rate", "0"),
        ("employee_rate", "1.01"),
        ("employee_rate", "abc"),
        ("employee_rate", 0.12),
        ("eps_rate", "-0.1"),
        ("wage_ceiling", "0"),
        ("eps_wage_ceiling", "-1"),
        ("optional_components", ["B"]),  # overlaps required
        ("unexpected", 1),
    ],
)
def test_invalid_pf_rules_rejected(tmp_path, path, value):
    data = copy.deepcopy(RULES_YAML)
    data["tenants"][0]["pf"][path] = value
    with pytest.raises(RulesError):
        load_payroll_rules(rules_file(tmp_path, data))


@pytest.mark.parametrize("value", ["1", "0.0001"])
def test_rate_boundaries_accepted(tmp_path, value):
    data = copy.deepcopy(RULES_YAML)
    data["tenants"][0]["pf"]["employee_rate"] = value
    assert load_payroll_rules(rules_file(tmp_path, data)).for_tenant("acme").pf.employee_rate == Decimal(value)


def test_missing_rules_file_rejected(tmp_path):
    with pytest.raises(RulesError):
        load_payroll_rules(tmp_path / "nope.yaml")


# Fingerprint ----------------------------------------------------------------------------------------------


def test_same_structure_same_fingerprint():
    assert slip_fingerprint(slip()) == slip_fingerprint(slip(basic=99999, da=1))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("gross_pay"),
        lambda s: s.__setitem__("gross_pay", "20000"),  # number -> string
        lambda s: s["earnings"][0].__setitem__("amount", "12000"),
        lambda s: s["earnings"][0].__setitem__("abbr", None),
        lambda s: s["earnings"][1].pop("abbr"),  # missing in one row
    ],
)
def test_structural_changes_change_the_fingerprint(mutate):
    changed = slip()
    mutate(changed)
    assert slip_fingerprint(changed) != slip_fingerprint(slip())


def test_unread_fields_do_not_affect_the_fingerprint():
    changed = slip()
    changed["some_new_frappe_field"] = 1
    changed["earnings"][0]["year_to_date"] = 1
    assert slip_fingerprint(changed) == slip_fingerprint(slip())


def test_fingerprint_format():
    assert slip_fingerprint(slip()).startswith("sha256:") and len(slip_fingerprint(slip())) == 71


# Validation -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,code",
    [
        (slip(currency="AED"), "INCONSISTENT_PAYLOAD"),
        (slip(basic=-1), "INCONSISTENT_PAYLOAD"),
        (slip(gross=20000.02 + 0.0), "INCONSISTENT_PAYLOAD"),
    ],
)
def test_inconsistent_payloads_rejected(rules, payload, code):
    if payload["gross_pay"] == 20000.02:
        payload["gross_pay"] = 20000.02
    with pytest.raises(PfError) as info:
        compute_pf(payload, rules)
    assert info.value.code == code


def test_duplicate_component_rejected(rules):
    bad = slip(
        extra_earnings=[
            {
                "salary_component": "Basic 2",
                "abbr": "B",
                "amount": 1,
                "statistical_component": 0,
                "do_not_include_in_total": 0,
            }
        ]
    )
    with pytest.raises(PfError) as info:
        compute_pf(bad, rules)
    assert info.value.code == "INCONSISTENT_PAYLOAD"


def test_non_numeric_amount_rejected(rules):
    bad = slip()
    bad["earnings"][0]["amount"] = "12000"
    with pytest.raises(PfError) as info:
        compute_pf(bad, rules)
    assert info.value.code == "INCONSISTENT_PAYLOAD"


@pytest.mark.parametrize("delta,ok", [(0.01, True), (-0.01, True), (0.02, False), (-0.02, False)])
def test_gross_tolerance_boundary(rules, delta, ok):
    payload = slip()
    payload["gross_pay"] = round(payload["gross_pay"] + delta, 2)
    if ok:
        compute_pf(payload, rules)
    else:
        with pytest.raises(PfError):
            compute_pf(payload, rules)


def test_statistical_and_excluded_rows_ignored_in_gross_check(rules):
    extras = [
        {
            "salary_component": "Stat",
            "abbr": "ST",
            "amount": 5000,
            "statistical_component": 1,
            "do_not_include_in_total": 0,
        },
        {
            "salary_component": "Excl",
            "abbr": "EX",
            "amount": 700,
            "statistical_component": 0,
            "do_not_include_in_total": 1,
        },
    ]
    payload = slip(extra_earnings=extras)
    assert payload["gross_pay"] == 20000
    compute_pf(payload, rules)


def test_missing_required_component(rules):
    payload = slip()
    payload["earnings"] = [e for e in payload["earnings"] if e["abbr"] != "B"]
    payload["gross_pay"] = 8000
    with pytest.raises(PfError) as info:
        compute_pf(payload, rules)
    assert info.value.code == "PF_COMPONENT_MISSING"


def test_missing_optional_component_counts_as_zero(rules):
    payload = slip()
    payload["earnings"] = [e for e in payload["earnings"] if e["abbr"] != "DA"]
    payload["gross_pay"] = 18000
    assert compute_pf(payload, rules)["pf_wage"] == "12000"


# Computation (EPF 12%/12%, EPS 8.33% capped at 15,000) ----------------------------------------------------


@pytest.mark.parametrize(
    "basic,da,expected",
    [
        # wage, employee, employer_total, eps, employer_epf
        (10000, 2000, ("12000", "1440", "1440", "1000", "440")),  # below ceiling: EPS 999.6 -> 1000
        (13000, 2000, ("15000", "1800", "1800", "1250", "550")),  # at ceiling: EPS 1249.5 -> 1250 (ROUND_HALF_UP)
        (20000, 5000, ("25000", "1800", "1800", "1250", "550")),  # above ceiling, both capped
    ],
)
def test_contributions(rules, basic, da, expected):
    result = compute_pf(slip(basic=basic, da=da), rules)
    assert (
        result["pf_wage"],
        result["employee_contribution"],
        result["employer_total"],
        result["employer_eps"],
        result["employer_epf"],
    ) == expected


def test_uncapped_employee_and_employer(tmp_path):
    data = copy.deepcopy(RULES_YAML)
    data["tenants"][0]["pf"].update(apply_ceiling_to_employee=False, apply_ceiling_to_employer=False)
    rules = load_payroll_rules(rules_file(tmp_path, data)).for_tenant("acme")
    result = compute_pf(slip(basic=20000, da=5000), rules)
    assert (
        result["employee_contribution"],
        result["employer_total"],
        result["employer_eps"],
        result["employer_epf"],
    ) == ("3000", "3000", "1250", "1750")  # EPS stays capped at 15,000 wage


def test_rounding_half_up(rules):
    # wage 12345.50 -> employee 1481.46 -> 1481 ; eps 1028.38 -> 1028
    result = compute_pf(slip(basic=12345.5, da=0), rules)
    assert result["employee_contribution"] == "1481" and result["employer_eps"] == "1028"


def test_provenance_and_no_name(rules):
    result = compute_pf(slip(), rules)
    assert result["provenance"]["document"] == "Sal Slip/HR-EMP-00001/00007"
    assert result["provenance"]["rules_version"] == "2026-10-acme-1"
    assert result["provenance"]["components"] == [
        {"abbr": "B", "salary_component": "Basic", "amount": "12000"},
        {"abbr": "DA", "salary_component": "Dearness Allowance", "amount": "2000"},
    ]
    assert "Sunita" not in str(result)


def test_exact_half_rupee_rounds_up_not_to_even(rules):
    # wage 8337.50 -> employee 0.12 * 8337.50 = 1000.50 -> 1001 (banker's rounding would give 1000)
    assert compute_pf(slip(basic=8337.5, da=0), rules)["employee_contribution"] == "1001"


def test_employee_capped_employer_on_full_wage(tmp_path):
    """Common voluntary arrangement: employee at the ₹15,000 ceiling, employer on actual wage."""
    data = copy.deepcopy(RULES_YAML)
    data["tenants"][0]["pf"].update(apply_ceiling_to_employee=True, apply_ceiling_to_employer=False)
    rules = load_payroll_rules(rules_file(tmp_path, data)).for_tenant("acme")
    result = compute_pf(slip(basic=20000, da=5000), rules)
    assert (
        result["employee_contribution"],
        result["employer_total"],
        result["employer_eps"],
        result["employer_epf"],
    ) == ("1800", "3000", "1250", "1750")
