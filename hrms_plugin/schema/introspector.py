"""Schema Introspector: Automated schema discovery for host HRMS platforms.

Supports OpenAPI 3.x, Swagger 2.0 (YAML/JSON), and black-box JSON record harvesting.
Extracts entities, endpoints, properties, types, nullability, and enum constraints.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

__all__ = [
    "FieldDataType",
    "DiscoveredField",
    "DiscoveredEntity",
    "IntrospectionResult",
    "SchemaIntrospector",
]


class FieldDataType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    DATE = "date"
    ARRAY = "array"
    OBJECT = "object"
    ANY = "any"


class DiscoveredField(BaseModel):
    name: str
    path: str
    data_type: FieldDataType = FieldDataType.STRING
    is_required: bool = False
    is_nullable: bool = True
    enum_values: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    format: Optional[str] = None
    sample_values: List[Any] = Field(default_factory=list)


class DiscoveredEntity(BaseModel):
    name: str
    read_endpoint: Optional[str] = None
    write_endpoint: Optional[str] = None
    id_field: Optional[str] = None
    fields: Dict[str, DiscoveredField] = Field(default_factory=dict)
    raw_sample: Dict[str, Any] = Field(default_factory=dict)

    def get_field(self, field_name: str) -> Optional[DiscoveredField]:
        return self.fields.get(field_name)


class IntrospectionResult(BaseModel):
    title: str = ""
    version: str = ""
    entities: Dict[str, DiscoveredEntity] = Field(default_factory=dict)
    endpoints_count: int = 0

    def get_entity(self, name: str) -> Optional[DiscoveredEntity]:
        name_lower = name.lower()
        for k, v in self.entities.items():
            if k.lower() == name_lower:
                return v
        return None


class SchemaIntrospector:
    """Discovers and parses schema structures from Swagger, OpenAPI, or raw JSON samples."""

    @classmethod
    def introspect_file(cls, file_path: str | Path) -> IntrospectionResult:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Schema file not found: {file_path}")

        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            spec = yaml.safe_load(content)
        else:
            spec = json.loads(content)
        return cls.introspect_openapi(spec)

    @classmethod
    def introspect_openapi(cls, spec: Dict[str, Any]) -> IntrospectionResult:
        info = spec.get("info", {})
        result = IntrospectionResult(
            title=str(info.get("title", "Unknown HRMS API")),
            version=str(info.get("version", "1.0.0")),
        )

        # Collect schemas / definitions
        schemas = {}
        if "components" in spec and "schemas" in spec["components"]:
            schemas = spec["components"]["schemas"]
        elif "definitions" in spec:
            schemas = spec["definitions"]

        # Parse declared schema models
        for model_name, model_def in schemas.items():
            if not isinstance(model_def, dict):
                continue
            entity = cls._parse_schema_model(model_name, model_def, schemas)
            result.entities[model_name] = entity

        # Inspect paths to match read/write endpoints
        paths = spec.get("paths", {})
        result.endpoints_count = len(paths)

        for path_url, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            entity_hint = cls._guess_entity_name_from_path(path_url)

            if "get" in path_item:
                get_op = path_item["get"]
                target_entity = cls._find_or_create_entity(result, entity_hint, get_op, schemas)
                if target_entity and not target_entity.read_endpoint:
                    target_entity.read_endpoint = f"GET {path_url}"

            for method in ("post", "put", "patch"):
                if method in path_item:
                    op = path_item[method]
                    target_entity = cls._find_or_create_entity(result, entity_hint, op, schemas)
                    if target_entity and not target_entity.write_endpoint:
                        target_entity.write_endpoint = f"{method.upper()} {path_url}"

        return result

    @classmethod
    def introspect_json_samples(cls, entity_name: str, samples: List[Dict[str, Any]]) -> DiscoveredEntity:
        if not samples:
            return DiscoveredEntity(name=entity_name)

        entity = DiscoveredEntity(name=entity_name, raw_sample=samples[0])
        all_keys = set()
        for sample in samples:
            if isinstance(sample, dict):
                all_keys.update(sample.keys())

        for key in sorted(all_keys):
            field_obj = cls._infer_field_from_samples(key, samples)
            entity.fields[key] = field_obj

        # 1. Direct standard ID candidates
        for id_candidate in (
            "id",
            "emp_id",
            "empId",
            "employee_id",
            "employeeId",
            f"{entity_name.lower()}_id",
            f"{entity_name.lower()}Id",
            "code",
            "uuid",
        ):
            if id_candidate in entity.fields:
                entity.id_field = id_candidate
                break

        # 2. Suffix heuristic matching (_id or Id) if still not identified
        if not entity.id_field:
            for f_name in entity.fields:
                if f_name.endswith("_id") or f_name.endswith("Id") or f_name.startswith("id_"):
                    entity.id_field = f_name
                    break

        return entity

    @classmethod
    def _parse_schema_model(
        cls,
        model_name: str,
        model_def: Dict[str, Any],
        all_schemas: Dict[str, Any],
    ) -> DiscoveredEntity:
        entity = DiscoveredEntity(name=model_name)
        required_fields = set(model_def.get("required", []))
        properties = model_def.get("properties", {})

        for prop_name, prop_spec in properties.items():
            if not isinstance(prop_spec, dict):
                continue

            resolved = cls._resolve_ref(prop_spec, all_schemas)
            data_type = cls._map_openapi_type(
                resolved.get("type", "string"),
                resolved.get("format"),
            )
            is_nullable = resolved.get("nullable", False)
            enum_values = [str(e) for e in resolved.get("enum", [])]
            description = resolved.get("description")
            fmt = resolved.get("format")

            sample_vals = []
            if "example" in resolved:
                sample_vals.append(resolved["example"])
            elif "examples" in resolved and isinstance(resolved["examples"], list):
                sample_vals.extend(resolved["examples"][:3])

            entity.fields[prop_name] = DiscoveredField(
                name=prop_name,
                path=prop_name,
                data_type=data_type,
                is_required=(prop_name in required_fields),
                is_nullable=is_nullable,
                enum_values=enum_values,
                description=description,
                format=fmt,
                sample_values=sample_vals,
            )

        for pk in ("id", f"{model_name.lower()}Id", f"{model_name.lower()}_id", "uuid"):
            if pk in entity.fields:
                entity.id_field = pk
                break

        return entity

    @classmethod
    def _resolve_ref(cls, spec: Dict[str, Any], all_schemas: Dict[str, Any]) -> Dict[str, Any]:
        if "$ref" not in spec:
            return spec
        ref_path = spec["$ref"]
        parts = ref_path.split("/")
        target_name = parts[-1]
        target_schema = all_schemas.get(target_name, {})
        merged = dict(target_schema)
        merged.update({k: v for k, v in spec.items() if k != "$ref"})
        return merged

    @classmethod
    def _map_openapi_type(cls, oas_type: str, oas_format: Optional[str]) -> FieldDataType:
        if oas_format in ("date-time", "datetime"):
            return FieldDataType.DATETIME
        if oas_format == "date":
            return FieldDataType.DATE
        if oas_type in ("integer", "int"):
            return FieldDataType.INTEGER
        if oas_type in ("number", "float", "double"):
            return FieldDataType.NUMBER
        if oas_type in ("boolean", "bool"):
            return FieldDataType.BOOLEAN
        if oas_type == "array":
            return FieldDataType.ARRAY
        if oas_type == "object":
            return FieldDataType.OBJECT
        return FieldDataType.STRING

    @classmethod
    def _guess_entity_name_from_path(cls, path_url: str) -> str:
        segments = [s for s in path_url.strip("/").split("/") if s and not s.startswith("{")]
        if not segments:
            return "Root"
        candidate = segments[0]
        if candidate.endswith("ies"):
            candidate = candidate[:-3] + "y"
        elif candidate.endswith("s") and not candidate.endswith("ss"):
            candidate = candidate[:-1]
        return candidate.capitalize()

    @classmethod
    def _find_or_create_entity(
        cls,
        result: IntrospectionResult,
        hint: str,
        op: Dict[str, Any],
        schemas: Dict[str, Any],
    ) -> Optional[DiscoveredEntity]:
        tags = op.get("tags", [])
        if tags and tags[0]:
            tag_name = tags[0]
            if tag_name in result.entities:
                return result.entities[tag_name]

        existing = result.get_entity(hint)
        if existing:
            return existing

        return None

    @classmethod
    def _infer_field_from_samples(cls, key: str, samples: List[Dict[str, Any]]) -> DiscoveredField:
        non_null_values = []
        is_nullable = False

        for sample in samples:
            val = sample.get(key)
            if val is None:
                is_nullable = True
            else:
                non_null_values.append(val)

        data_type = cls._infer_data_type(non_null_values)
        unique_vals = sorted({str(v) for v in non_null_values if v is not None})
        enum_values = unique_vals if (0 < len(unique_vals) <= 12 and len(non_null_values) >= 1) else []

        return DiscoveredField(
            name=key,
            path=key,
            data_type=data_type,
            is_required=(not is_nullable),
            is_nullable=is_nullable,
            enum_values=enum_values,
            sample_values=non_null_values[:3],
        )

    @classmethod
    def _infer_data_type(cls, values: List[Any]) -> FieldDataType:
        if not values:
            return FieldDataType.STRING

        first = values[0]
        if isinstance(first, bool):
            return FieldDataType.BOOLEAN
        if isinstance(first, int):
            return FieldDataType.INTEGER
        if isinstance(first, (float, Decimal)):
            return FieldDataType.NUMBER
        if isinstance(first, list):
            return FieldDataType.ARRAY
        if isinstance(first, dict):
            return FieldDataType.OBJECT
        if isinstance(first, (datetime, date)):
            return FieldDataType.DATETIME

        if isinstance(first, str):
            if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", first):
                return FieldDataType.DATETIME
            if re.match(r"^\d{4}-\d{2}-\d{2}$", first):
                return FieldDataType.DATE
            try:
                int(first)
                return FieldDataType.INTEGER
            except ValueError:
                pass
            try:
                float(first)
                return FieldDataType.NUMBER
            except ValueError:
                pass

        return FieldDataType.STRING
