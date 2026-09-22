"""Dynamic REST Connector for executing queries and mutations against any HRMS."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Type

import httpx

from hrms_plugin.connectors.base import (
    BaseHRMSConnector,
    ConnectorConfig,
    HRMSAuthError,
    HRMSConflictError,
    HRMSConnectorError,
    HRMSNotFoundError,
    HRMSTimeoutError,
    HRMSValidationError,
)
from hrms_plugin.connectors.concurrency import IdempotencyManager, compute_idempotency_key
from hrms_plugin.connectors.profiles import VendorProfile, get_vendor_profile
from hrms_plugin.schema.canonical import (
    CanonicalAsset,
    CanonicalCandidate,
    CanonicalEmployee,
    CanonicalEntity,
    CanonicalLeaveRequest,
    CanonicalPunch,
    CanonicalRequisition,
    EntityType,
    SourceRef,
)
from hrms_plugin.schema.mapping import EntityMapping

logger = logging.getLogger(__name__)

_ENTITY_CLASS_MAP: Dict[EntityType, Type[CanonicalEntity]] = {
    EntityType.EMPLOYEE: CanonicalEmployee,
    EntityType.REQUISITION: CanonicalRequisition,
    EntityType.CANDIDATE: CanonicalCandidate,
    EntityType.LEAVE_REQUEST: CanonicalLeaveRequest,
    EntityType.PUNCH: CanonicalPunch,
    EntityType.ASSET: CanonicalAsset,
}


class DynamicRESTConnector(BaseHRMSConnector):
    """Executes live queries and mutations against host HRMS endpoints.

    Driven by dynamic EntityMapping definitions and vendor profiles, ensuring
    type-safe bi-directional conversion between host wire formats and Canonical entities.
    """

    def __init__(
        self,
        config: ConnectorConfig,
        profile: Optional[VendorProfile] = None,
        mappings: Optional[Dict[EntityType, EntityMapping]] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(config)
        self.profile = profile or get_vendor_profile(config.vendor_name)
        self.mappings = mappings or {}
        self.idempotency = idempotency_manager or IdempotencyManager()

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=httpx.Timeout(self.config.timeout_seconds),
                verify=self.config.verify_ssl,
                headers=self.config.default_headers,
            )
            self._owns_client = True

    async def close(self) -> None:
        """Close the underlying HTTP client session if owned."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> DynamicRESTConnector:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _send_request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Execute an HTTP request with retry logic for transient errors."""
        url = path if path.startswith("http") else f"{self.config.base_url}{path}"
        max_attempts = max(1, self.config.max_retries)
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    params=params,
                )

                # Retry on transient server errors (502, 503, 504, 429)
                if response.status_code in (429, 502, 503, 504) and attempt < max_attempts:
                    backoff = self.config.backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        "[connector:%s] transient status %d on %s %s (attempt %d/%d). Retrying in %.2fs...",
                        self.config.vendor_name,
                        response.status_code,
                        method,
                        url,
                        attempt,
                        max_attempts,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                return response

            except (httpx.ConnectError, httpx.ReadError) as net_err:
                last_error = net_err
                if attempt < max_attempts:
                    backoff = self.config.backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        "[connector:%s] network error on %s %s (attempt %d/%d): %s. Retrying in %.2fs...",
                        self.config.vendor_name,
                        method,
                        url,
                        attempt,
                        max_attempts,
                        net_err,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
            except httpx.TimeoutException as timeout_err:
                raise HRMSTimeoutError(
                    message=f"Request to {method} {url} timed out after {self.config.timeout_seconds}s",
                    vendor=self.config.vendor_name,
                ) from timeout_err

        if last_error:
            raise HRMSConnectorError(
                message=f"Network connection failed after {max_attempts} attempts: {last_error}",
                vendor=self.config.vendor_name,
            ) from last_error

        raise HRMSConnectorError(
            message=f"Failed to execute request {method} {url}",
            vendor=self.config.vendor_name,
        )

    def _handle_response_status(self, response: httpx.Response, action: str, entity_type: EntityType) -> Any:
        """Parse JSON response and raise structured domain exceptions for 4xx/5xx status codes."""
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}

        if response.is_success:
            return body

        status = response.status_code
        err_msg = self.profile.extract_error_message(
            body,
            default=f"HTTP {status} {response.reason_phrase} during {action} on {entity_type.value}",
        )

        vendor = self.config.vendor_name
        if status in (401, 403):
            raise HRMSAuthError(message=err_msg, status_code=status, response_body=body, vendor=vendor)
        elif status == 404:
            raise HRMSNotFoundError(message=err_msg, status_code=status, response_body=body, vendor=vendor)
        elif status == 409:
            raise HRMSConflictError(message=err_msg, status_code=status, response_body=body, vendor=vendor)
        elif status in (400, 422):
            raise HRMSValidationError(message=err_msg, status_code=status, response_body=body, vendor=vendor)
        else:
            raise HRMSConnectorError(message=err_msg, status_code=status, response_body=body, vendor=vendor)

    def _to_canonical(
        self,
        raw_dict: Dict[str, Any],
        entity_type: EntityType,
        source_id: Optional[str] = None,
    ) -> CanonicalEntity:
        """Transform a vendor record dictionary into a validated Canonical entity dataclass."""
        mapping = self.mappings.get(entity_type)
        if mapping:
            canonical_data = mapping.to_canonical(raw_dict)
            try:
                source_id = source_id or mapping.external_id(raw_dict)
            except Exception:
                pass
        else:
            canonical_data = dict(raw_dict)

        cls = _ENTITY_CLASS_MAP.get(entity_type)
        if not cls:
            raise ValueError(f"Unknown Canonical entity class for {entity_type}")

        # Ensure tenant_id is populated
        canonical_data.setdefault("tenant_id", self.config.vendor_name)

        # Ensure source_ref is populated
        if "source_ref" not in canonical_data or not canonical_data["source_ref"]:
            canonical_data["source_ref"] = SourceRef(
                vendor=self.config.vendor_name,
                entity_type=entity_type,
                external_id=str(source_id or raw_dict.get("id") or raw_dict.get("job_id") or "unknown"),
                version=str(raw_dict.get("version") or raw_dict.get("updatedAt") or "1"),
            )

        # Filter keys to match target dataclass fields
        valid_keys = cls.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        filtered = {k: v for k, v in canonical_data.items() if k in valid_keys}
        return cls(**filtered)

    async def fetch_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Fetch a single entity by its primary identifier."""
        path = self.profile.endpoint_for(entity_type, "get", entity_id=entity_id, auth_token=auth_token, **kwargs)
        headers = self.profile.prepare_headers(auth_token=auth_token)

        response = await self._send_request(method="GET", path=path, headers=headers)
        body = self._handle_response_status(response, action="get", entity_type=entity_type)
        unwrapped = self.profile.unwrap_response(body, action="get", entity_type=entity_type)

        if not isinstance(unwrapped, dict):
            raise HRMSConnectorError(
                message=f"Expected dictionary payload for {entity_type.value} '{entity_id}', got {type(unwrapped)}",
                vendor=self.config.vendor_name,
            )

        return self._to_canonical(unwrapped, entity_type, source_id=entity_id)

    async def list_entities(
        self,
        entity_type: EntityType,
        filters: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, int]] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> List[CanonicalEntity]:
        """List entities of a given type with optional filtering and pagination."""
        path = self.profile.endpoint_for(entity_type, "list", auth_token=auth_token, **kwargs)
        headers = self.profile.prepare_headers(auth_token=auth_token)
        params = dict(filters or {})
        if pagination:
            params.update(pagination)

        response = await self._send_request(method="GET", path=path, headers=headers, params=params)
        body = self._handle_response_status(response, action="list", entity_type=entity_type)
        unwrapped = self.profile.unwrap_response(body, action="list", entity_type=entity_type)

        if not isinstance(unwrapped, list):
            if isinstance(unwrapped, dict):
                # Check for common collection keys
                for k in ("items", "rows", "records", "data"):
                    if isinstance(unwrapped.get(k), list):
                        unwrapped = unwrapped[k]
                        break
            if not isinstance(unwrapped, list):
                unwrapped = [unwrapped] if unwrapped else []

        results: List[CanonicalEntity] = []
        for item in unwrapped:
            if isinstance(item, dict):
                results.append(self._to_canonical(item, entity_type))

        return results

    async def create_entity(
        self,
        entity_type: EntityType,
        canonical_payload: Dict[str, Any],
        auth_token: Optional[str] = None,
        actor_id: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Create a new entity in the host HRMS from a canonical payload with idempotency protection."""
        idempotency_key = compute_idempotency_key(
            entity_type=entity_type,
            action="create",
            payload=canonical_payload,
            actor_id=actor_id,
            tenant_id=self.config.vendor_name,
        )

        acquired, cached_result = await self.idempotency.acquire_or_get(idempotency_key)
        if not acquired and cached_result is not None:
            logger.info(
                "[connector:%s] returning cached result for idempotency key %s",
                self.config.vendor_name,
                idempotency_key[:12],
            )
            return cached_result

        path = self.profile.endpoint_for(entity_type, "create", auth_token=auth_token, **kwargs)
        headers = self.profile.prepare_headers(auth_token=auth_token)
        wire_payload = self.profile.normalize_payload_for_host(
            canonical_payload=canonical_payload,
            entity_type=entity_type,
            action="create",
            auth_token=auth_token,
        )

        try:
            response = await self._send_request(method="POST", path=path, headers=headers, json_data=wire_payload)
            body = self._handle_response_status(response, action="create", entity_type=entity_type)
            unwrapped = self.profile.unwrap_response(body, action="create", entity_type=entity_type)

            created_record = unwrapped if isinstance(unwrapped, dict) else body
            entity = self._to_canonical(created_record, entity_type)

            self.idempotency.store_result(idempotency_key, entity)
            return entity

        except Exception:
            self.idempotency.release_on_failure(idempotency_key)
            raise

    async def update_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        patch_payload: Dict[str, Any],
        auth_token: Optional[str] = None,
        expected_version: Optional[str] = None,
        **kwargs: Any,
    ) -> CanonicalEntity:
        """Update an existing entity with optional optimistic version checking."""
        path = self.profile.endpoint_for(entity_type, "update", entity_id=entity_id, auth_token=auth_token, **kwargs)
        headers = self.profile.prepare_headers(auth_token=auth_token)
        if expected_version:
            headers["If-Match"] = expected_version

        wire_payload = self.profile.normalize_payload_for_host(
            canonical_payload=patch_payload,
            entity_type=entity_type,
            action="update",
            auth_token=auth_token,
        )

        response = await self._send_request(method="PATCH", path=path, headers=headers, json_data=wire_payload)
        body = self._handle_response_status(response, action="update", entity_type=entity_type)
        unwrapped = self.profile.unwrap_response(body, action="update", entity_type=entity_type)

        updated_record = unwrapped if isinstance(unwrapped, dict) else body
        return self._to_canonical(updated_record, entity_type, source_id=entity_id)

    async def delete_entity(
        self,
        entity_type: EntityType,
        entity_id: str,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """Delete an entity in the host HRMS."""
        path = self.profile.endpoint_for(entity_type, "delete", entity_id=entity_id, auth_token=auth_token, **kwargs)
        headers = self.profile.prepare_headers(auth_token=auth_token)

        response = await self._send_request(method="DELETE", path=path, headers=headers)
        self._handle_response_status(response, action="delete", entity_type=entity_type)
        return True
