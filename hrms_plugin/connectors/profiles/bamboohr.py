"""Host profile for BambooHR REST API v1.

Handles BambooHR company domain routing, Basic Auth API key encoding,
and directory/time_off envelope unwrapping.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

logger = logging.getLogger(__name__)


class BambooHRProfile(VendorProfile):
    """Vendor profile for BambooHR REST API."""

    vendor_name: str = "bamboohr"

    def __init__(
        self,
        company_domain: str = "api",
        api_version: str = "v1",
    ) -> None:
        self.company_domain = company_domain
        self.api_version = api_version

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        act = action.lower()

        if entity_type == EntityType.EMPLOYEE:
            if act in ("list", "search", "directory"):
                return f"/{self.api_version}/employees/directory"
            if entity_id:
                return f"/{self.api_version}/employees/{entity_id}"
            return f"/{self.api_version}/employees"

        elif entity_type == EntityType.LEAVE_REQUEST:
            if entity_id and act in ("get", "fetch"):
                return f"/{self.api_version}/time_off/requests/{entity_id}"
            return f"/{self.api_version}/time_off/requests"

        elif entity_type == EntityType.REQUISITION:
            if entity_id and act in ("get", "fetch"):
                return f"/{self.api_version}/applicant_tracking/jobs/{entity_id}"
            return f"/{self.api_version}/applicant_tracking/jobs"

        elif entity_type == EntityType.CANDIDATE:
            if entity_id and act in ("get", "fetch"):
                return f"/{self.api_version}/applicant_tracking/applications/{entity_id}"
            return f"/{self.api_version}/applicant_tracking/applications"

        elif entity_type == EntityType.PUNCH:
            return f"/{self.api_version}/time_tracking/clock"

        plural = entity_type.value.lower() + "s"
        return f"/{self.api_version}/{plural}/{entity_id}" if entity_id else f"/{self.api_version}/{plural}"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hrms-agentic-plugin/1.0 (+bamboohr-profile)",
        }
        if auth_token:
            if auth_token.startswith("Basic ") or auth_token.startswith("Bearer "):
                headers["Authorization"] = auth_token
            else:
                # BambooHR uses API key as username with dummy password 'x'
                b64_key = base64.b64encode(f"{auth_token}:x".encode()).decode()
                headers["Authorization"] = f"Basic {b64_key}"
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
            if "employees" in response_data:
                return response_data["employees"]
            if "requests" in response_data:
                return response_data["requests"]
            if "jobs" in response_data:
                return response_data["jobs"]
            if "applications" in response_data:
                return response_data["applications"]
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
            if "full_name" in payload and "firstName" not in payload:
                parts = str(payload["full_name"]).split(maxsplit=1)
                payload["firstName"] = parts[0]
                payload["lastName"] = parts[1] if len(parts) > 1 else ""

        elif entity_type == EntityType.LEAVE_REQUEST:
            if "start_date" in payload and "start" not in payload:
                payload["start"] = payload["start_date"]
            if "end_date" in payload and "end" not in payload:
                payload["end"] = payload["end_date"]

        return payload
