"""Apply a tenant's approved mapping over a vendor profile, field by field (Q1 resolution order, batch QC).

Order: approved tenant mapping > vendor profile default. Pending proposals are never applied. A FieldMap is only
translated when the declarative connector can honour it exactly; anything else is refused (OverlayError), because
silently falling back to the vendor default would ignore what the tenant approved.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from pydantic import ValidationError

from hrms_plugin.integrations.declarative import INT_FIELDS, REQUISITION_FIELDS, EntitySpec, FieldSpec
from hrms_plugin.schema.mapping import EntityMapping, FieldMap

_TRANSFORM_TYPES = {"string": "str", "int": "int"}


class OverlayError(Exception):
    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def _field_spec(fm: FieldMap) -> FieldSpec:
    if fm.target not in REQUISITION_FIELDS:
        raise OverlayError(fm.target, "not a requisition field")
    if not isinstance(fm.source, str):
        raise OverlayError(fm.target, "the connector reads a single source path")
    if fm.transform not in _TRANSFORM_TYPES:
        raise OverlayError(fm.target, f"transform {fm.transform!r} is not supported by the connector")
    if fm.default is not None:
        raise OverlayError(fm.target, "a default value cannot be applied by the connector")
    if fm.required:
        raise OverlayError(fm.target, "required is not supported by the connector")
    expected = "int" if fm.target in INT_FIELDS else "str"
    if _TRANSFORM_TYPES[fm.transform] != expected:
        raise OverlayError(fm.target, f"type must be {expected}")
    data: Dict[str, object] = {"from": fm.source, "type": expected}
    if fm.values:
        data["map"] = dict(fm.values)
    try:
        return FieldSpec.model_validate(data)
    except ValidationError:
        raise OverlayError(fm.target, f"invalid source path {fm.source!r}") from None


def overlay(spec: EntitySpec, mapping: EntityMapping) -> Tuple[EntitySpec, List[str]]:
    fields = dict(spec.fields)
    overridden: List[str] = []
    for fm in mapping.fields:
        fields[fm.target] = _field_spec(fm)  # type: ignore[index]
        overridden.append(fm.target)
    try:
        merged = EntitySpec.model_validate({**spec.model_dump(by_alias=True), "fields": {
            name: fs.model_dump(by_alias=True, exclude_none=True) for name, fs in fields.items()}})
    except ValidationError as exc:
        message = "; ".join(str(e["msg"]) for e in exc.errors())
        raise OverlayError(overridden[0] if len(overridden) == 1 else ",".join(overridden), message) from None
    return merged, overridden
