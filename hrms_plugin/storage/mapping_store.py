"""Persistent storage and registry for compiled EntityMapping definitions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.storage.base import BaseKeyValueStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore


def entity_mapping_to_dict(mapping: EntityMapping) -> Dict[str, Any]:
    """Serialize an immutable EntityMapping instance into a JSON-compatible dict."""
    fields_list = []
    for f in mapping.fields:
        fields_list.append(
            {
                "target": f.target,
                "source": list(f.source) if isinstance(f.source, (list, tuple)) else f.source,
                "transform": f.transform,
                "default": f.default,
                "required": f.required,
                "values": dict(f.values) if f.values else {},
            }
        )

    return {
        "vendor": mapping.vendor,
        "entity_type": mapping.entity_type,
        "id_path": mapping.id_path,
        "version_path": mapping.version_path,
        "collection_path": mapping.collection_path,
        "writeback": dict(mapping.writeback) if mapping.writeback else {},
        "fields": fields_list,
    }


def entity_mapping_from_dict(data: Dict[str, Any]) -> EntityMapping:
    """Reconstitute an EntityMapping from a JSON-compatible dictionary."""
    field_maps = []
    for fd in data.get("fields", []):
        src = fd.get("source")
        if isinstance(src, list):
            src = tuple(src)
        fm = FieldMap(
            target=str(fd["target"]),
            source=src,
            transform=str(fd.get("transform", "string")),
            default=fd.get("default"),
            required=bool(fd.get("required", False)),
            values=dict(fd.get("values", {})),
        )
        field_maps.append(fm)

    return EntityMapping(
        vendor=str(data["vendor"]),
        entity_type=str(data["entity_type"]),
        id_path=str(data["id_path"]),
        version_path=data.get("version_path"),
        collection_path=data.get("collection_path"),
        writeback=dict(data.get("writeback", {})),
        fields=tuple(field_maps),
    )


class MappingStore:
    """Persistent registry for tenant-specific compiled schema mappings."""

    NAMESPACE = "entity_mappings"

    def __init__(self, backend: Optional[BaseKeyValueStore] = None) -> None:
        self.backend: BaseKeyValueStore = backend or SqliteKeyValueStore()

    @staticmethod
    def _make_key(tenant_id: str, vendor: str, entity_type: str | EntityType) -> str:
        ent = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type).upper()
        return f"{tenant_id}:{vendor.lower()}:{ent}"

    def save_mapping(
        self,
        tenant_id: str,
        mapping: EntityMapping,
    ) -> None:
        """Persist an EntityMapping for a tenant and vendor."""
        key = self._make_key(tenant_id, mapping.vendor, mapping.entity_type)
        data = entity_mapping_to_dict(mapping)
        self.backend.set(self.NAMESPACE, key, data)

    def load_mapping(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
    ) -> Optional[EntityMapping]:
        """Load a persisted EntityMapping, or return None if not yet synthesized."""
        key = self._make_key(tenant_id, vendor, entity_type)
        data = self.backend.get(self.NAMESPACE, key)
        if not data:
            return None
        try:
            return entity_mapping_from_dict(data)
        except Exception:
            return None

    def list_mappings(self, tenant_id: str) -> List[Dict[str, str]]:
        """List all synthesized mappings available for a tenant."""
        all_keys = self.backend.list_keys(self.NAMESPACE)
        prefix = f"{tenant_id}:"
        results = []
        for k in all_keys:
            if k.startswith(prefix):
                parts = k.split(":")
                if len(parts) >= 3:
                    results.append({"vendor": parts[1], "entity_type": parts[2]})
        return results

    def clear(self) -> None:
        self.backend.clear(self.NAMESPACE)
