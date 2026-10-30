"""Two-phase, human-approved, idempotent write-back with rollback (piece F2, decisions W1-W9)."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Optional, Tuple

from hrms_plugin.integrations.frappe import FrappeClient, FrappeError
from hrms_plugin.integrations.host_registry import HostConfig, HostRegistryError, load_host_registry_from_env
from hrms_plugin.security.audit import AuditSigningError, HostAuditGateway
from hrms_plugin.security.rbac import AuthContext
from hrms_plugin.storage.base import BaseKeyValueStore

NAMESPACE = "writeback_proposals"
PROPOSAL_TTL_SECONDS = 24 * 3600
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ClientFactory = Callable[[HostConfig, str, str], FrappeClient]


class WritebackError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _same(host_value: Any, new_value: Any, kind: str) -> bool:
    """Compare a host value with a proposed one; decimals compare numerically (host sends 1500.5, we hold "1500.50")."""
    if kind == "decimal":
        if isinstance(host_value, bool) or not isinstance(host_value, (int, float, str)):
            return False
        try:
            return Decimal(str(host_value)) == Decimal(new_value)
        except InvalidOperation:
            return False
    return host_value == new_value


def _valid(value: Any, kind: str) -> bool:
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str) and len(value) <= 140
    if kind == "date":
        if not isinstance(value, str) or not _DATE.match(value):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if kind == "decimal":
        if not isinstance(value, str):
            return False
        try:
            return Decimal(value).is_finite()
        except InvalidOperation:
            return False
    return False


class WritebackService:
    def __init__(
        self,
        store: BaseKeyValueStore,
        client_factory: Optional[ClientFactory] = None,
        clock: Callable[[], float] = time.time,
        audit: Optional[HostAuditGateway] = None,
    ):
        self._store = store
        self._client_factory = client_factory or (lambda host, key, secret: FrappeClient(host, key, secret))
        self._clock = clock
        self._audit = audit or HostAuditGateway()
        self._locks: Dict[str, asyncio.Lock] = {}

    # helpers ----------------------------------------------------------------------------------------
    def _host(self, tenant_id: str) -> Tuple[HostConfig, str, str]:
        try:
            host = load_host_registry_from_env().for_tenant(tenant_id)
            if host is None:
                raise WritebackError("HOST_NOT_CONFIGURED")
            if host.vendor != "frappe":
                raise WritebackError("VENDOR_NOT_SUPPORTED")  # write-back is verified for Frappe only (piece I6)
            key, secret = host.credentials()
        except HostRegistryError:
            raise WritebackError("HOST_NOT_CONFIGURED") from None
        return host, key, secret

    async def _call(self, tenant_id: str, fn: Callable[[FrappeClient], Any]) -> Any:
        host, key, secret = self._host(tenant_id)
        client = self._client_factory(host, key, secret)
        try:
            return await fn(client)
        except FrappeError as exc:
            raise WritebackError("HOST_ERROR", exc.detail or exc.code) from None
        finally:
            await client.aclose()

    def _save(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        self._store.set(NAMESPACE, f"{proposal['tenant_id']}:{proposal['id']}", proposal)
        return proposal

    async def get(self, auth: AuthContext, proposal_id: str) -> Dict[str, Any]:
        proposal = self._store.get(NAMESPACE, f"{auth.tenant_id}:{proposal_id}")
        if not proposal:
            raise WritebackError("NOT_FOUND")
        return proposal

    # phase 1 -----------------------------------------------------------------------------------------
    async def propose(self, auth: AuthContext, doctype: str, name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        host, _key, _secret = self._host(auth.tenant_id)
        allowed = host.writable_fields.get(doctype)
        if not allowed:
            raise WritebackError("FIELD_NOT_WRITABLE", f"{doctype} is not writable for this tenant")
        if not isinstance(patch, dict) or not patch:
            raise WritebackError("INVALID_VALUE", "patch must change at least one field")
        for field, value in patch.items():
            if field not in allowed:
                raise WritebackError("FIELD_NOT_WRITABLE", f"{doctype}.{field}")
            if not _valid(value, allowed[field]):
                raise WritebackError("INVALID_VALUE", f"{doctype}.{field} must be {allowed[field]}")
        document = await self._call(auth.tenant_id, lambda c: c.get_document(doctype, name))
        base_modified = document.get("modified")
        if not isinstance(base_modified, str) or not base_modified:
            raise WritebackError("HOST_ERROR", "document has no modified timestamp")
        diff = [{"field": f, "before": document.get(f), "after": v} for f, v in sorted(patch.items())
                if not _same(document.get(f), v, allowed[f])]
        if not diff:
            raise WritebackError("NO_CHANGE")
        now = self._clock()
        proposal = {
            "id": secrets.token_hex(16),
            "tenant_id": auth.tenant_id,
            "doctype": doctype,
            "name": name,
            "patch": {d["field"]: d["after"] for d in diff},
            "before": {d["field"]: d["before"] for d in diff},
            "diff": diff,
            "base_modified": base_modified,
            "status": "PENDING",
            "proposed_by": auth.user_id,
            "proposed_at": now,
            "expires_at": now + PROPOSAL_TTL_SECONDS,
        }
        return self._save(proposal)

    # phase 2 -----------------------------------------------------------------------------------------
    async def approve(self, auth: AuthContext, proposal_id: str) -> Dict[str, Any]:
        async with self._locks.setdefault(f"{auth.tenant_id}:{proposal_id}", asyncio.Lock()):
            proposal = await self.get(auth, proposal_id)
            if proposal["status"] == "EXECUTED":
                return proposal
            if proposal["status"] != "PENDING":
                raise WritebackError("INVALID_STATE", proposal["status"])
            if auth.user_id == proposal["proposed_by"]:
                raise WritebackError("SAME_PERSON", "the approver must not be the proposer")
            if self._clock() > proposal["expires_at"]:
                proposal["status"] = "EXPIRED"
                self._save(proposal)
                raise WritebackError("EXPIRED")
            if not self._audit.is_configured():
                raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED")
            current = await self._call(auth.tenant_id, lambda c: c.get_document(proposal["doctype"], proposal["name"]))
            if current.get("modified") != proposal["base_modified"]:
                proposal["status"] = "CONFLICT"
                self._save(proposal)
                raise WritebackError("CONFLICT", "the record changed after the proposal was made")
            headers = self._audit.create_attribution_headers(
                tenant_id=auth.tenant_id,
                agent_name="WritebackService",
                hitl_approver_id=auth.user_id,
                reasoning_payload={"proposal": proposal_id, "patch": proposal["patch"]},
            ).to_http_headers()
            updated = await self._call(
                auth.tenant_id,
                lambda c: c.update_document(proposal["doctype"], proposal["name"], proposal["patch"], headers),
            )
            after_modified = updated.get("modified")
            if not isinstance(after_modified, str) or not after_modified:
                # The write already happened: never fall back to PENDING (that would allow a second write).
                try:
                    reread = await self._call(
                        auth.tenant_id, lambda c: c.get_document(proposal["doctype"], proposal["name"])
                    )
                    after_modified = reread.get("modified") if isinstance(reread.get("modified"), str) else None
                except WritebackError:
                    after_modified = None
            proposal.update(status="EXECUTED", after_modified=after_modified, approved_by=auth.user_id,
                            approved_at=self._clock(), audit=headers)
            return self._save(proposal)

    async def rollback(self, auth: AuthContext, proposal_id: str) -> Dict[str, Any]:
        async with self._locks.setdefault(f"{auth.tenant_id}:{proposal_id}", asyncio.Lock()):
            proposal = await self.get(auth, proposal_id)
            if proposal["status"] == "ROLLED_BACK":
                return proposal
            if proposal["status"] != "EXECUTED":
                raise WritebackError("INVALID_STATE", proposal["status"])
            if not proposal.get("after_modified"):
                raise WritebackError("ROLLBACK_UNAVAILABLE", "the host did not report the post-write version")

            if not proposal.get("rollback_requested_by"):
                proposal["rollback_requested_by"] = auth.user_id
                proposal["rollback_requested_at"] = self._clock()
                self._save(proposal)
                return {**proposal, "status": "ROLLBACK_PENDING"}

            if proposal.get("rollback_requested_by") == auth.user_id:
                raise WritebackError("SAME_PERSON", "the confirmer must not be the requester")

            if not self._audit.is_configured():
                raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED")

            current = await self._call(auth.tenant_id, lambda c: c.get_document(proposal["doctype"], proposal["name"]))
            if current.get("modified") != proposal.get("after_modified"):
                raise WritebackError("ROLLBACK_CONFLICT", "the record changed after our write")
            headers = self._audit.create_attribution_headers(
                tenant_id=auth.tenant_id,
                agent_name="WritebackService",
                hitl_approver_id=auth.user_id,
                reasoning_payload={"proposal": proposal_id, "rollback": proposal["before"]},
            ).to_http_headers()
            await self._call(
                auth.tenant_id,
                lambda c: c.update_document(proposal["doctype"], proposal["name"], proposal["before"], headers),
            )
            proposal.update(status="ROLLED_BACK", rolled_back_by=auth.user_id, rolled_back_at=self._clock(),
                            rollback_audit=headers)
            return self._save(proposal)
