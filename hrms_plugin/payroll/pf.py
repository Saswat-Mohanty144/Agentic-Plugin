"""Indian EPF/EPS contribution from a submitted Frappe Salary Slip (piece F1, decisions F1-1, F1-8, F1-9, F1-10)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List

from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.payroll.rules import TenantPayrollRules

GROSS_TOLERANCE = Decimal("0.01")
_RUPEE = Decimal("1")


class PfError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _amount(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PfError("INCONSISTENT_PAYLOAD", f"{where} is not a number")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise PfError("INCONSISTENT_PAYLOAD", f"{where} must be a non-negative number")
    return number


def _round(value: Decimal) -> Decimal:
    return value.quantize(_RUPEE, rounding=ROUND_HALF_UP)


def compute_pf(slip: Dict[str, Any], rules: TenantPayrollRules) -> Dict[str, Any]:
    if slip.get("currency") != rules.currency:
        raise PfError("INCONSISTENT_PAYLOAD", f"currency {slip.get('currency')!r} != rules currency {rules.currency}")
    earnings = slip.get("earnings")
    if not isinstance(earnings, list):
        raise PfError("INCONSISTENT_PAYLOAD", "earnings missing")

    by_abbr: Dict[str, Dict[str, Any]] = {}
    counted = Decimal(0)
    for i, row in enumerate(earnings):
        abbr = row.get("abbr") if isinstance(row, dict) else None
        if not isinstance(abbr, str) or not abbr:
            raise PfError("INCONSISTENT_PAYLOAD", f"earnings[{i}].abbr missing")
        if abbr in by_abbr:
            raise PfError("INCONSISTENT_PAYLOAD", f"duplicate component {abbr!r}")
        amount = _amount(row.get("amount"), f"earnings[{i}].amount")
        by_abbr[abbr] = {"row": row, "amount": amount}
        if not row.get("statistical_component") and not row.get("do_not_include_in_total"):
            counted += amount

    gross = _amount(slip.get("gross_pay"), "gross_pay")
    if abs(counted - gross) > GROSS_TOLERANCE:
        raise PfError("INCONSISTENT_PAYLOAD", f"earnings total {counted} != gross_pay {gross}")

    pf = rules.pf
    missing = [c for c in pf.required_components if c not in by_abbr]
    if missing:
        raise PfError("PF_COMPONENT_MISSING", f"required components absent: {missing}")

    used: List[Dict[str, str]] = []
    wage = Decimal(0)
    for abbr in (*pf.required_components, *pf.optional_components):
        if abbr in by_abbr:
            entry = by_abbr[abbr]
            wage += entry["amount"]
            used.append({"abbr": abbr, "salary_component": str(entry["row"].get("salary_component", "")),
                         "amount": str(entry["amount"])})

    employee_base = min(wage, pf.wage_ceiling) if pf.apply_ceiling_to_employee else wage
    employer_base = min(wage, pf.wage_ceiling) if pf.apply_ceiling_to_employer else wage
    employee = _round(employee_base * pf.employee_rate)
    employer_total = _round(employer_base * pf.employer_rate)
    eps = _round(min(wage, pf.eps_wage_ceiling) * pf.eps_rate)
    return {
        "employee_id": slip.get("employee"),
        "currency": rules.currency,
        "pf_wage": str(wage),
        "employee_contribution": str(employee),
        "employer_total": str(employer_total),
        "employer_eps": str(eps),
        "employer_epf": str(employer_total - eps),
        "provenance": {
            "vendor": "frappe",
            "doctype": "Salary Slip",
            "document": slip.get("name"),
            "modified": slip.get("modified"),
            "fingerprint": slip_fingerprint(slip),
            "rules_version": rules.rules_version,
            "components": used,
        },
    }
