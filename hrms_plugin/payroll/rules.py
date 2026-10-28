"""Per-tenant payroll rules (piece F1, decision F1-4). Values are customer/legal inputs; never hard-coded."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Optional, Tuple, Union

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

RULES_ENV_VAR = "HRMS_PAYROLL_RULES"


class RulesError(Exception):
    """Payroll rules are missing or invalid."""


def _decimal(value: str, *, minimum_exclusive: Decimal, maximum: Optional[Decimal] = None) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"not a decimal: {value!r}") from None
    if not number.is_finite() or number <= minimum_exclusive or (maximum is not None and number > maximum):
        raise ValueError(f"out of range: {value!r}")
    return number


class PfRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_components: Tuple[StrictStr, ...] = Field(min_length=1)
    optional_components: Tuple[StrictStr, ...] = ()
    employee_rate: Decimal
    employer_rate: Decimal
    eps_rate: Decimal
    wage_ceiling: Decimal
    eps_wage_ceiling: Decimal
    apply_ceiling_to_employee: StrictBool
    apply_ceiling_to_employer: StrictBool

    @field_validator("employee_rate", "employer_rate", "eps_rate", mode="before")
    @classmethod
    def _rate(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError("rates must be decimal strings, e.g. \"0.12\"")
        return _decimal(value, minimum_exclusive=Decimal(0), maximum=Decimal(1))

    @field_validator("wage_ceiling", "eps_wage_ceiling", mode="before")
    @classmethod
    def _ceiling(cls, value: object) -> Decimal:
        if not isinstance(value, str):
            raise ValueError("ceilings must be decimal strings, e.g. \"15000\"")
        return _decimal(value, minimum_exclusive=Decimal(0))

    @model_validator(mode="after")
    def _disjoint(self) -> "PfRules":
        for name in ("required_components", "optional_components"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} contains duplicates")
        if self.eps_rate > self.employer_rate:
            raise ValueError("eps_rate cannot exceed employer_rate (EPS is part of the employer contribution)")
        if self.eps_wage_ceiling > self.wage_ceiling:
            raise ValueError("eps_wage_ceiling cannot exceed wage_ceiling")
        overlap = set(self.required_components) & set(self.optional_components)
        if overlap:
            raise ValueError(f"components cannot be both required and optional: {sorted(overlap)}")
        return self


class TenantPayrollRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    rules_version: StrictStr = Field(min_length=1)
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")
    pf: PfRules


class PayrollRulesRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenants: Tuple[TenantPayrollRules, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "PayrollRulesRegistry":
        ids = [t.tenant_id for t in self.tenants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tenant_id in payroll rules")
        return self

    def for_tenant(self, tenant_id: str) -> Optional[TenantPayrollRules]:
        return next((t for t in self.tenants if t.tenant_id == tenant_id), None)


def load_payroll_rules(path: Union[str, Path]) -> PayrollRulesRegistry:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RulesError(f"cannot read payroll rules {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RulesError(f"invalid YAML in payroll rules {path}: {exc}") from exc
    try:
        return PayrollRulesRegistry.model_validate(data)
    except ValidationError as exc:
        raise RulesError(f"invalid payroll rules {path}: {exc}") from exc


def load_payroll_rules_from_env(env: Optional[Mapping[str, str]] = None) -> PayrollRulesRegistry:
    env = os.environ if env is None else env
    path = env.get(RULES_ENV_VAR)
    if not path:
        raise RulesError(f"{RULES_ENV_VAR} is not set")
    return load_payroll_rules(path)


__all__ = ["PayrollRulesRegistry", "PfRules", "RulesError", "TenantPayrollRules", "load_payroll_rules",
           "load_payroll_rules_from_env"]
