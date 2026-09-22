"""Base protocol and class for vendor profiles."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from hrms_plugin.schema.canonical import EntityType


class VendorProfile(ABC):
    """Encapsulates host-specific quirks, path routing, and payload conversions."""

    vendor_name: str = "base"

    @abstractmethod
    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Return the relative URL path for the requested entity and action."""
        ...

    @abstractmethod
    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Construct vendor-compliant HTTP headers including authentication and origin."""
        ...

    @abstractmethod
    def unwrap_response(
        self,
        response_data: Any,
        action: str,
        entity_type: EntityType,
    ) -> Any:
        """Extract the core domain entity payload from vendor response envelopes."""
        ...

    @abstractmethod
    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert a canonical payload into the vendor's expected wire format."""
        ...

    def extract_error_message(self, response_data: Any, default: str) -> str:
        """Extract a human-readable error message from a vendor error response body."""
        if isinstance(response_data, dict):
            for k in ("error_message", "message", "error", "detail", "status"):
                val = response_data.get(k)
                if val and isinstance(val, str):
                    return val
        return default
