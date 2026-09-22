"""Connectors package for hrms-agentic-plugin: Universal REST, Concurrency, and Profiles."""

from __future__ import annotations

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
from hrms_plugin.connectors.compensation import (
    CompensatingStep,
    RollbackResult,
    SagaCoordinator,
)
from hrms_plugin.connectors.concurrency import (
    IdempotencyManager,
    OptimisticConcurrencyManager,
    compute_idempotency_key,
)
from hrms_plugin.connectors.dynamic_rest import DynamicRESTConnector
from hrms_plugin.connectors.profiles import (
    GenericProfile,
    IceiptsProfile,
    VendorProfile,
    extract_jwt_user_id,
    get_vendor_profile,
)

__all__ = [
    "BaseHRMSConnector",
    "ConnectorConfig",
    "HRMSConnectorError",
    "HRMSAuthError",
    "HRMSNotFoundError",
    "HRMSValidationError",
    "HRMSConflictError",
    "HRMSTimeoutError",
    "DynamicRESTConnector",
    "IdempotencyManager",
    "OptimisticConcurrencyManager",
    "compute_idempotency_key",
    "SagaCoordinator",
    "CompensatingStep",
    "RollbackResult",
    "VendorProfile",
    "GenericProfile",
    "IceiptsProfile",
    "extract_jwt_user_id",
    "get_vendor_profile",
]
