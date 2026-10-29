"""Canonical salary facts with unit, period and currency (Q1/Q2, batch QB).

"Salary" is never one field: each amount below is monthly unless named annual, in the tenant's payroll currency.
A host payload that produces inconsistent facts is refused, never coerced.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

# The currency a jurisdiction's statutory calculations are defined in.
JURISDICTION_CURRENCY = {"IN": "INR", "AE": "AED"}
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class SalaryFactsError(Exception):
    """The facts derived from a host payload are inconsistent; nothing is calculated from them."""


class SalaryFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jurisdiction: str
    currency: str
    gross_monthly: Decimal
    pf_wage_monthly: Decimal
    basic_monthly: Optional[Decimal] = None
    dearness_allowance_monthly: Optional[Decimal] = None
    annual_ctc: Optional[Decimal] = None
    period: Optional[str] = None  # YYYY-MM the amounts belong to

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise SalaryFactsError("; ".join(str(e["msg"]) for e in exc.errors())) from None

    @field_validator("period")
    @classmethod
    def _period(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _PERIOD.match(value):
            raise ValueError(f"period must be YYYY-MM, got {value!r}")
        return value

    @field_validator("gross_monthly", "pf_wage_monthly", "basic_monthly", "dearness_allowance_monthly", "annual_ctc")
    @classmethod
    def _amount(cls, value: Optional[Decimal], info) -> Optional[Decimal]:
        if value is None:
            return value
        if not value.is_finite():
            raise ValueError(f"{info.field_name} is not finite")
        if value < 0:
            raise ValueError(f"{info.field_name} is negative")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "SalaryFacts":
        expected = JURISDICTION_CURRENCY.get(self.jurisdiction)
        if expected != self.currency:
            raise ValueError(f"currency {self.currency} does not match jurisdiction {self.jurisdiction}")
        if self.pf_wage_monthly > self.gross_monthly:
            raise ValueError("pf_wage_monthly exceeds gross_monthly")
        if self.basic_monthly is not None:
            fixed = self.basic_monthly + (self.dearness_allowance_monthly or Decimal(0))
            if fixed > self.gross_monthly:
                raise ValueError("basic_monthly + dearness_allowance_monthly exceeds gross_monthly")
        if self.annual_ctc is not None and self.gross_monthly * 12 > self.annual_ctc:
            raise ValueError("gross_monthly x 12 exceeds annual_ctc")
        return self


def previous_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
