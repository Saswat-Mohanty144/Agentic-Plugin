"""Base interfaces, configuration, and exception hierarchy for HRMS REST connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hrms_plugin.schema.canonical import CanonicalEntity, EntityType


class HRMSConnectorError(Exception):
    """Base exception for all HRMS connector errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
        vendor: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.vendor = vendor


class HRMSAuthError(HRMSConnectorError):
    """Raised when authentication/authorization fails (HTTP 401/403)."""


class HRMSNotFoundError(HRMSConnectorError):
    """Raised when the requested resource does not exist (HTTP 404)."""


class HRMSValidationError(HRMSConnectorError):
    """Raised when the host HRMS rejects a request payload due to schema/business validation (HTTP 400/422)."""


class HRMSConflictError(HRMSConnectorError):
    """Raised when an optimistic concurrency or duplicate conflict occurs (HTTP 409)."""


class HRMSTimeoutError(HRMSConnectorError):
    """Raised when an HTTP request times out."""


@dataclass
class ConnectorConfig:
    """Configuration for an HRMS connector instance."""

    base_url: str
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_factor: float = 0.5
    default_headers: Dict[str, str] = field(default_factory=dict)
    vendor_name: str = "generic"
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")


class BaseHRMSConnector(ABC):
    """Abstract base class for all host HRMS connectors."""

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config

    @abstractmethod
    async def fetch_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Fetch a single entity by its primary identifier."""
        ...

    @abstractmethod
    async def list_entities(
        self,
        entity_type: EntityType,
        filters: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, int]] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> List[CanonicalEntity]:
        """List entities of a given type with optional filtering and pagination."""
        ...

    @abstractmethod
    async def create_entity(
        self,
        entity_type: EntityType,
        canonical_payload: Dict[str, Any],
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Create a new entity in the host HRMS from a canonical payload."""
        ...

    @abstractmethod
    async def update_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        patch_payload: Dict[str, Any],
        auth_token: Optional[str] = None,
        expected_version: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Update an existing entity with optimistic concurrency checking."""
        ...

    @abstractmethod
    async def delete_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """Delete an entity in the host HRMS."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close any open network client sessions or connection pools."""
        ...
