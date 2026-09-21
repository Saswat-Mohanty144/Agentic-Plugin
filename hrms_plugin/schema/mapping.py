"""Declarative field mapping engine between vendor payloads and the canonical domain models.

Provides sub-millisecond path resolution, type transformations, enum translation,
and bidirectional projection (read from vendor, writeback to vendor).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

__all__ = [
    "FieldMap",
    "EntityMapping",
    "resolve_path",
    "TRANSFORMS",
    "MappingError",
]


class MappingError(Exception):
    """Raised when a payload cannot be mapped into or out of canonical structure."""


_INDEX = re.compile(r"^(?P<name>[^\[\]]*)\[(?P<idx>\d*)\]$")


def resolve_path(payload: Any, path: str) -> Any:
    """Traverse payload by a dotted path with array projection support. Returns None when absent."""
    if not path:
        return None

    segments = path.split(".")
    current = payload

    for position, segment in enumerate(segments):
        if current is None:
            return None
        match = _INDEX.match(segment)
        if match:
            name, idx = match.group("name"), match.group("idx")
            if name:
                current = current.get(name) if isinstance(current, dict) else None
            if current is None:
                return None
            if not isinstance(current, (list, tuple)):
                return None
            if idx == "":
                remainder = ".".join(segments[position + 1 :])
                if not remainder:
                    return list(current)
                projected = [resolve_path(item, remainder) for item in current]
                return [v for v in projected if v not in (None, "")]
            index = int(idx)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            return parser(text)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def _to_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = _to_datetime(value)
    return parsed.date() if parsed else None


def _to_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;|]", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, dict):
                picked = next(
                    (str(v) for v in item.values() if isinstance(v, (str, int, float))),
                    None,
                )
                if picked:
                    out.append(picked)
            elif item is not None:
                out.append(str(item))
        return out
    return [str(value)]


def _join_names(value: Any) -> Optional[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if v and str(v).strip()]
        return " ".join(parts) or None
    return str(value).strip() or None if value is not None else None


TRANSFORMS: Dict[str, Callable[[Any], Any]] = {
    "string": lambda v: None if v is None else str(v),
    "strip": lambda v: None if v is None else str(v).strip(),
    "lower": lambda v: None if v is None else str(v).lower(),
    "upper": lambda v: None if v is None else str(v).upper(),
    "int": lambda v: int(v) if str(v or "").strip().lstrip("-").isdigit() else None,
    "decimal": _to_decimal,
    "datetime": _to_datetime,
    "date": _to_date,
    "list": _to_list,
    "join_names": _join_names,
    "bool": lambda v: bool(v) if v is not None else None,
}


@dataclass(frozen=True)
class FieldMap:
    """One canonical field, sourced from one or more vendor paths."""

    target: str
    source: Union[str, Tuple[str, ...]]
    transform: str = "string"
    default: Any = None
    required: bool = False
    values: Dict[str, str] = field(default_factory=dict)

    def apply(self, payload: Dict[str, Any]) -> Any:
        paths = (self.source,) if isinstance(self.source, str) else tuple(self.source)
        if len(paths) > 1 and self.transform == "join_names":
            raw: Any = [resolve_path(payload, p) for p in paths]
        else:
            raw = None
            for path in paths:
                candidate = resolve_path(payload, path)
                if candidate not in (None, "", [], {}):
                    raw = candidate
                    break

        fn = TRANSFORMS.get(self.transform)
        if fn is None:
            raise MappingError(
                f"{self.target}: unknown transform {self.transform!r}. Available: {sorted(TRANSFORMS)}"
            )
        value = fn(raw)

        if value is not None and self.values:
            key = str(value)
            value = self.values.get(key, self.values.get(key.upper(), value))

        if value in (None, "", []):
            value = self.default
        if self.required and value in (None, "", []):
            raise MappingError(
                f"required field {self.target!r} is empty; looked in {list(paths)}"
            )
        return value


@dataclass(frozen=True)
class EntityMapping:
    """A vendor's mapping for one canonical entity."""

    vendor: str
    entity_type: str
    id_path: str
    version_path: Optional[str] = None
    fields: Tuple[FieldMap, ...] = ()
    collection_path: Optional[str] = None
    writeback: Dict[str, str] = field(default_factory=dict)

    def external_id(self, payload: Dict[str, Any]) -> str:
        value = resolve_path(payload, self.id_path)
        if value in (None, ""):
            raise MappingError(
                f"{self.vendor}/{self.entity_type}: no id at path {self.id_path!r}."
            )
        return str(value)

    def version(self, payload: Dict[str, Any]) -> Optional[str]:
        if not self.version_path:
            return None
        value = resolve_path(payload, self.version_path)
        return str(value) if value not in (None, "") else None

    def to_canonical(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field_map in self.fields:
            out[field_map.target] = field_map.apply(payload)
        return out

    def records(self, response: Any) -> List[Dict[str, Any]]:
        """Pull the record list out of a vendor's response envelope."""
        if self.collection_path:
            found = resolve_path(response, self.collection_path)
            if found is None:
                return []
            return list(found) if isinstance(found, (list, tuple)) else [found]
        if isinstance(response, list):
            return list(response)
        return [response] if isinstance(response, dict) else []

    def to_vendor(self, canonical: Dict[str, Any]) -> Dict[str, Any]:
        """Project canonical fields back onto vendor write paths."""
        if not self.writeback:
            raise MappingError(
                f"{self.vendor}/{self.entity_type} is read-only: no writeback mapping"
            )
        payload: Dict[str, Any] = {}
        for canonical_field, vendor_path in self.writeback.items():
            if canonical_field not in canonical:
                continue
            value = canonical[canonical_field]
            if isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, (datetime, date)):
                value = value.isoformat()
            cursor = payload
            parts = vendor_path.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
        return payload
