"""PF calculation service: host data in, fail-closed checks, provenance out (piece F1)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from hrms_plugin.integrations.frappe import SALARY_SLIP, FrappeClient, FrappeError
from hrms_plugin.integrations.host_registry import HostConfig, HostRegistryError, load_host_registry_from_env
from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.payroll.pf import PfError, compute_pf
from hrms_plugin.payroll.rules import RulesError, load_payroll_rules_from_env
from hrms_plugin.storage.base import BaseKeyValueStore

logger = logging.getLogger(__name__)

APPROVED_NS = "approved_fingerprints"
QUARANTINE_NS = "quarantine"
VENDOR = "frappe"
_EMPLOYEE_ID = re.compile(r"^[A-Za-z0-9._/ -]{1,140}$")
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

ClientFactory = Callable[[HostConfig, str, str], FrappeClient]


def _error(code: str, **extra: Any) -> Dict[str, Any]:
    return {"error": code, **extra}


class PayrollService:
    def __init__(
        self,
        store: BaseKeyValueStore,
        client_factory: Optional[ClientFactory] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._client_factory = client_factory or (lambda host, key, secret: FrappeClient(host, key, secret))
        self._clock = clock

    @staticmethod
    def _key(tenant_id: str, vendor: str, doctype: str) -> str:
        return f"{tenant_id}:{vendor}:{doctype}"

    def approve_fingerprint(
        self, tenant_id: str, vendor: str, doctype: str, fingerprint: str, approved_by: str
    ) -> Dict:
        record = {"fingerprint": fingerprint, "approved_by": approved_by, "approved_at": self._clock()}
        self._store.set(APPROVED_NS, self._key(tenant_id, vendor, doctype), record)
        return record

    def list_quarantine(self, tenant_id: str) -> List[Dict[str, Any]]:
        prefix = f"{tenant_id}:"
        return [row for key, row in sorted(self._store.get_all(QUARANTINE_NS).items()) if key.startswith(prefix)]

    async def calculate_pf(self, tenant_id: str, employee_id: str, period: str, jurisdiction: str) -> Dict[str, Any]:
        if not isinstance(employee_id, str) or not _EMPLOYEE_ID.match(employee_id):
            return _error("INVALID_ARGUMENT", detail="employee_id")
        if not isinstance(period, str) or not _PERIOD.match(period):
            return _error("INVALID_ARGUMENT", detail="period must be YYYY-MM")
        if jurisdiction != "IN":
            return _error("NOT_APPLICABLE", detail="PF applies to Indian payroll (jurisdiction IN) only")
        try:
            host = load_host_registry_from_env().for_tenant(tenant_id)
            if host is None:
                return _error("HOST_NOT_CONFIGURED")
            key, secret = host.credentials()
        except HostRegistryError:
            return _error("HOST_NOT_CONFIGURED")
        try:
            rules = load_payroll_rules_from_env().for_tenant(tenant_id)
        except RulesError:
            rules = None
        if rules is None:
            return _error("RULES_NOT_CONFIGURED")

        client = self._client_factory(host, key, secret)
        try:
            slip = await client.fetch_submitted_salary_slip(employee_id, period)
        except FrappeError as exc:
            return _error(exc.code)
        finally:
            await client.aclose()

        observed = slip_fingerprint(slip)
        approved = self._store.get(APPROVED_NS, self._key(tenant_id, VENDOR, SALARY_SLIP))
        if not approved:
            return _error("MAPPING_NOT_APPROVED", observed_fingerprint=observed)
        if approved.get("fingerprint") != observed:
            row = {"vendor": VENDOR, "doctype": SALARY_SLIP, "document": slip.get("name"),
                   "observed_fingerprint": observed, "expected_fingerprint": approved.get("fingerprint"),
                   "quarantined_at": self._clock()}
            self._store.set(QUARANTINE_NS, f"{self._key(tenant_id, VENDOR, SALARY_SLIP)}:{slip.get('name')}", row)
            logger.warning("schema drift quarantined", extra={"reason": "SCHEMA_DRIFT", "tenant": tenant_id})
            return _error("SCHEMA_DRIFT", observed_fingerprint=observed)
        try:
            return compute_pf(slip, rules)
        except PfError as exc:
            return _error(exc.code, detail=exc.detail)
