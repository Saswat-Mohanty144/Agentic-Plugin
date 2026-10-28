"""Host profile for Workday REST / RaaS APIs.

Handles Workday tenant routing paths (/ccx/api/v1/{tenant}/...),
OAuth2 Bearer token authentication, and Report_Entry / data collection unwrapping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

logger = logging.getLogger(__name__)


class WorkdayProfile(VendorProfile):
    """Vendor profile for Workday REST APIs."""

    vendor_name: str = "workday"

    def __init__(
        self,
        tenant_name: str = "default_tenant",
        api_prefix: str = "/ccx/api/v1",
    ) -> None:
        self.tenant_name = tenant_name
        self.api_prefix = api_prefix.rstrip("/")

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        act = action.lower()
        tenant = kwargs.get("tenant") or self.tenant_name
        base = f"{self.api_prefix}/{tenant}"

        if entity_type == EntityType.EMPLOYEE:
            if entity_id and act in ("get", "fetch", "update"):
                return f"{base}/workers/{entity_id}"
            return f"{base}/workers"

        elif entity_type == EntityType.LEAVE_REQUEST:
            if entity_id and act in ("get", "fetch"):
                return f"{base}/timeOffRequests/{entity_id}"
            return f"{base}/timeOffRequests"

        elif entity_type == EntityType.REQUISITION:
            if entity_id and act in ("get", "fetch"):
                return f"{base}/jobRequisitions/{entity_id}"
            return f"{base}/jobRequisitions"

        elif entity_type == EntityType.CANDIDATE:
            if entity_id and act in ("get", "fetch"):
                return f"{base}/jobApplications/{entity_id}"
            return f"{base}/jobApplications"

        elif entity_type == EntityType.PUNCH:
            return f"{base}/timeClockEvents"

        plural = entity_type.value.lower() + "s"
        return f"{base}/{plural}/{entity_id}" if entity_id else f"{base}/{plural}"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hrms-agentic-plugin/1.0 (+workday-profile)",
        }
        if auth_token:
            headers["Authorization"] = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
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
            if "Report_Entry" in response_data:
                return response_data["Report_Entry"]
            if "items" in response_data:
                return response_data["items"]
        return response_data

    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(canonical_payload)
        return payload
