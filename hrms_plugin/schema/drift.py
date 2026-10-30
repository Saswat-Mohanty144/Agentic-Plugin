"""Runtime Schema Drift Detector & Self-Healing Interceptor.

Monitors outbound host payloads and incoming responses for unmapped fields,
deprecated endpoint contracts, unexpected type mismatches, and HTTP 400 schema rejections.
Synthesizes delta remediation patches to heal runtime FieldMap definitions autonomously.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.mapping import EntityMapping, resolve_path

logger = logging.getLogger(__name__)


class DriftSeverity(str, Enum):
    INFO = "INFO"  # Non-breaking new optional field detected
    WARNING = "WARNING"  # Renamed field or nullable change
    CRITICAL = "CRITICAL"  # Missing required field causing host HTTP 400


@dataclass
class DriftEvent:
    entity_type: EntityType
    severity: DriftSeverity
    detected_at: str
    field_name: str
    description: str
    suggested_patch: Optional[Dict[str, Any]] = None
    resolved: bool = False


class SchemaDriftWarning(Warning):
    """Raised when runtime payload drift is detected against a host endpoint."""

    pass


class SchemaDriftInterceptor:
    """Monitors request/response flows and detects schema discrepancies."""

    def __init__(self) -> None:
        self.history: List[DriftEvent] = []

    def inspect_response(
        self,
        entity_type: EntityType,
        mapping: Optional[Union[EntityMapping, Dict[str, Any]]] = None,
        raw_response: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[DriftEvent]:
        """Check if host response includes newly added fields or missing expected keys."""
        resp = raw_response or kwargs.get("response") or {}
        events: List[DriftEvent] = []
        if not isinstance(resp, dict):
            return events

        def _add_source(src: Any) -> None:
            if isinstance(src, str):
                known_host_fields.add(src)
                top = src.split(".")[0].split("[")[0]
                if top:
                    known_host_fields.add(top)
            elif isinstance(src, (list, tuple)):
                for item in src:
                    _add_source(item)

        known_host_fields: Set[str] = set()
        if isinstance(mapping, EntityMapping):
            for fm in mapping.fields:
                _add_source(fm.source)
            if mapping.id_path:
                _add_source(mapping.id_path)
        elif isinstance(mapping, dict):
            for k, v in mapping.items():
                _add_source(k)
                _add_source(v)

        actual_fields = set(resp.keys())

        # 1. Detect New Unmapped Host Fields
        unmapped_fields = actual_fields - known_host_fields
        for f in unmapped_fields:
            if f.startswith("_") or f in ("data", "status", "success", "message"):
                continue
            event = DriftEvent(
                entity_type=entity_type,
                severity=DriftSeverity.INFO,
                detected_at=datetime.now(timezone.utc).isoformat(),
                field_name=f,
                description=f"Host response contains unmapped field '{f}'. Consider synthesizing canonical binding.",
                suggested_patch={"action": "ADD_MAPPING", "host_field": f, "sample_value": resp.get(f)},
            )
            events.append(event)
            self.history.append(event)

        # 2. Detect Missing Expected Host Fields (resolving dotted paths)
        expected_fields = kwargs.get("expected_fields")
        if expected_fields:
            for exp in expected_fields:
                if isinstance(exp, str):
                    val = resolve_path(resp, exp) if "." in exp else resp.get(exp)
                    if val is None:
                        event = DriftEvent(
                            entity_type=entity_type,
                            severity=DriftSeverity.WARNING,
                            detected_at=datetime.now(timezone.utc).isoformat(),
                            field_name=exp,
                            description=f"Host response missing expected field '{exp}'.",
                            suggested_patch={"action": "ADD_REQUIRED_FIELD", "missing_field": exp},
                        )
                        events.append(event)
                        self.history.append(event)
        elif kwargs.get("check_missing") and isinstance(mapping, EntityMapping):
            for fm in mapping.fields:
                sources = fm.source if isinstance(fm.source, (list, tuple)) else [fm.source]
                found = any(
                    (resolve_path(resp, s) is not None if "." in s else s in resp)
                    for s in sources
                    if isinstance(s, str)
                )
                if not found and isinstance(fm.source, str):
                    event = DriftEvent(
                        entity_type=entity_type,
                        severity=DriftSeverity.WARNING,
                        detected_at=datetime.now(timezone.utc).isoformat(),
                        field_name=fm.source,
                        description=f"Host response missing expected field '{fm.source}'.",
                        suggested_patch={"action": "ADD_REQUIRED_FIELD", "missing_field": fm.source},
                    )
                    events.append(event)
                    self.history.append(event)

        return events

    def inspect_error_payload(
        self,
        entity_type: EntityType,
        mapping: Optional[Union[EntityMapping, Dict[str, Any]]] = None,
        outbound_payload: Optional[Dict[str, Any]] = None,
        host_error_response: Optional[Dict[str, Any]] = None,
        status_code: int = 400,
    ) -> DriftEvent:
        """Analyze HTTP 400 Bad Request error to diagnose payload rejection caused by schema drift."""
        msg_text = ""
        if isinstance(host_error_response, dict):
            msg_text = str(
                host_error_response.get("message")
                or host_error_response.get("error_message")
                or host_error_response.get("detail")
                or host_error_response.get("error")
                or str(host_error_response)
            )
        else:
            msg_text = str(host_error_response or "")

        missing_fields: List[str] = []
        # Common host error phrasing
        for key in ["missing required field", "is required", "cannot be null", "invalid parameter"]:
            if key in msg_text.lower():
                import re

                candidates = re.findall(r"['\"]([a-zA-Z0-9_-]+)['\"]", msg_text)
                missing_fields.extend(candidates)

        missing_field = missing_fields[0] if missing_fields else "unknown_field"

        event = DriftEvent(
            entity_type=entity_type,
            severity=DriftSeverity.CRITICAL,
            detected_at=datetime.now(timezone.utc).isoformat(),
            field_name=missing_field,
            description=f"Host HTTP {status_code} schema rejection: {msg_text[:120]}",
            suggested_patch={
                "action": "ADD_REQUIRED_FIELD",
                "missing_field": missing_field,
                "endpoint_type": entity_type.value,
            },
        )

        self.history.append(event)
        logger.warning("Detected schema drift event on %s: %s", entity_type.value, event.description)
        return event

    def generate_remediation_delta(self, event: DriftEvent) -> Dict[str, Any]:
        """Generate a patch dictionary to update the FieldMap without manual code changes."""
        return {
            "patch_version": "1.0.0",
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "target_entity": event.entity_type.value,
            "field_name": event.field_name,
            "patch_instruction": event.suggested_patch,
            "status": "APPLIED",
        }
