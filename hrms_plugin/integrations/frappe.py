"""Minimal Frappe HRMS REST client for submitted Salary Slips (piece F1, decision F1-5).

Conventions (docs.frappe.io): `Authorization: token key:secret`; lists at /api/resource/:doctype with JSON
`filters`/`fields`, result under `data`; single documents at /api/resource/:doctype/:name under `data`.
"""

from __future__ import annotations

import calendar
import json
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from hrms_plugin.integrations.host_registry import HostConfig

SALARY_SLIP = "Salary Slip"


class FrappeError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class FrappeClient:
    def __init__(
        self, host: HostConfig, api_key: str, api_secret: str, http_client: Optional[httpx.AsyncClient] = None
    ):
        self._host = host
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=host.timeout_seconds)
        self._headers = {"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = await self._http.get(f"{self._host.base_url}{path}", params=params, headers=self._headers)
            response.raise_for_status()
            return response.json()["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise FrappeError("HOST_ERROR", f"{type(exc).__name__}") from None

    async def fetch_submitted_salary_slip(self, employee_id: str, period: str) -> Dict[str, Any]:
        year, month = (int(p) for p in period.split("-"))
        last_day = calendar.monthrange(year, month)[1]
        filters = [
            ["employee", "=", employee_id],
            ["start_date", ">=", f"{year:04d}-{month:02d}-01"],
            ["start_date", "<=", f"{year:04d}-{month:02d}-{last_day:02d}"],
            ["docstatus", "=", 1],
        ]
        listing = await self._get(
            f"/api/resource/{quote(SALARY_SLIP, safe='')}",
            {"filters": json.dumps(filters), "fields": json.dumps(["name"]), "limit_page_length": 2},
        )
        if not isinstance(listing, list):
            raise FrappeError("HOST_ERROR", "listing is not a list")
        if not listing:
            raise FrappeError("SLIP_NOT_FOUND", "no submitted salary slip for that employee and month")
        if len(listing) > 1:
            raise FrappeError("AMBIGUOUS_SLIPS", "more than one submitted salary slip for that employee and month")
        name = listing[0].get("name") if isinstance(listing[0], dict) else None
        if not isinstance(name, str) or not name:
            raise FrappeError("HOST_ERROR", "listing entry without a name")
        document = await self._get(f"/api/resource/{quote(SALARY_SLIP, safe='')}/{quote(name, safe='')}")
        if not isinstance(document, dict):
            raise FrappeError("HOST_ERROR", "document is not an object")
        # Never trust the second call blindly: the document must be the submitted slip we asked for (agy F7).
        start = document.get("start_date")
        if (
            document.get("docstatus") != 1
            or document.get("employee") != employee_id
            or not isinstance(start, str)
            or not (filters[1][2] <= start <= filters[2][2])
        ):
            raise FrappeError("INCONSISTENT_PAYLOAD", "document does not match the requested submitted slip")
        return document
