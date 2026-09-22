"""Generic REST profile adhering to standard RESTful conventions."""

from __future__ import annotations

from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

_DEFAULT_PLURALS: Dict[EntityType, str] = {
    EntityType.EMPLOYEE: "employees",
    EntityType.LEAVE_REQUEST: "leave-requests",
    EntityType.REQUISITION: "requisitions",
    EntityType.CANDIDATE: "candidates",
    EntityType.PUNCH: "punches",
    EntityType.ASSET: "assets",
}


class GenericProfile(VendorProfile):
    """Standard REST vendor profile used when no proprietary vendor profile is specified."""

    vendor_name: str = "generic"

    def __init__(self, plural_overrides: Optional[Dict[EntityType, str]] = None) -> None:
        self.plurals = dict(_DEFAULT_PLURALS)
        if plural_overrides:
            self.plurals.update(plural_overrides)

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        plural = self.plurals.get(entity_type, entity_type.value.lower() + "s")
        act = action.lower()
        if act in ("get", "fetch", "update", "patch", "delete"):
            if not entity_id:
                raise ValueError(f"Action '{action}' on {entity_type.value} requires an entity_id")
            return f"/{plural}/{entity_id}"
        elif act in ("list", "create", "post"):
            return f"/{plural}"
        return f"/{plural}"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if auth_token:
            auth_val = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
            headers["Authorization"] = auth_val
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
                inner = response_data["data"]
                if isinstance(inner, dict) and "items" in inner:
                    return inner["items"]
                return inner
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
        return canonical_payload
