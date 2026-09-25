"""Host profile for Frappe / ERPNext HRMS.

Supports Frappe DocType REST APIs, /api/resource/* endpoints, token authentication,
and Frappe data envelope conventions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

logger = logging.getLogger(__name__)


class FrappeProfile(VendorProfile):
    """Vendor profile for Frappe / ERPNext HR module."""

    vendor_name: str = "frappe"

    def __init__(
        self,
        company: str = "Standard Company",
        default_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.company = company
        self.default_headers = default_headers or {}

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        act = action.lower()

        doctype_map = {
            EntityType.EMPLOYEE: "Employee",
            EntityType.REQUISITION: "Job Requisition",
            EntityType.LEAVE_REQUEST: "Leave Application",
            EntityType.CANDIDATE: "Job Applicant",
            EntityType.PUNCH: "Employee Checkin",
        }

        doctype = doctype_map.get(entity_type, entity_type.value.capitalize())

        if act in ("get", "fetch", "update", "patch", "delete") and entity_id:
            return f"/api/resource/{doctype}/{entity_id}"
        return f"/api/resource/{doctype}"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hrms-agentic-plugin/1.0 (+frappe-profile)",
        }
        if auth_token:
            if auth_token.startswith("token ") or auth_token.startswith("Bearer "):
                headers["Authorization"] = auth_token
            else:
                headers["Authorization"] = f"token {auth_token}"
        headers.update(self.default_headers)
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def unwrap_response(
        self,
        response_data: Any,
        action: str,
        entity_type: EntityType,
    ) -> Any:
        if isinstance(response_data, dict):
            if "data" in response_data:
                return response_data["data"]
            if "message" in response_data and isinstance(response_data["message"], (dict, list)):
                return response_data["message"]
        return response_data

    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(canonical_payload)

        if entity_type == EntityType.EMPLOYEE:
            if "full_name" in payload and "first_name" not in payload:
                parts = str(payload["full_name"]).split(maxsplit=1)
                payload["first_name"] = parts[0]
                if len(parts) > 1:
                    payload["last_name"] = parts[1]
            if "company" not in payload:
                payload["company"] = self.company

        elif entity_type == EntityType.LEAVE_REQUEST:
            if "leave_type" in payload:
                payload["leave_type"] = str(payload["leave_type"]).title()
            if "from_date" in payload:
                payload["from_date"] = str(payload["from_date"])
            if "to_date" in payload:
                payload["to_date"] = str(payload["to_date"])

        elif entity_type == EntityType.REQUISITION:
            if "title" in payload and "designation" not in payload:
                payload["designation"] = payload["title"]
            if "company" not in payload:
                payload["company"] = self.company

        return payload
