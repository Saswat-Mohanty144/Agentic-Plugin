"""Persistent storage and registry for compiled EntityMapping definitions."""

from __future__ import annotations

import time
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


class MappingGovernanceError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


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
        self.backend.clear(self.VERSIONS_NAMESPACE)

    # Governance (piece G4): proposals are versioned; only an approved version becomes the active mapping ----------
    VERSIONS_NAMESPACE = "entity_mapping_versions"

    def _versions(self, tenant_id: str, vendor: str, entity_type: str | EntityType) -> Dict[int, Dict]:
        prefix = self._make_key(tenant_id, vendor, entity_type) + ":"
        return {
            int(key[len(prefix):]): row
            for key, row in self.backend.get_all(self.VERSIONS_NAMESPACE).items()
            if key.startswith(prefix)
        }

    def _put_version(self, tenant_id: str, vendor: str, entity_type: str | EntityType, row: Dict) -> None:
        key = f"{self._make_key(tenant_id, vendor, entity_type)}:{row['version']:06d}"
        self.backend.set(self.VERSIONS_NAMESPACE, key, row)

    def propose_mapping(self, tenant_id: str, mapping: EntityMapping, proposed_by: str) -> int:
        existing = self._versions(tenant_id, mapping.vendor, mapping.entity_type)
        version = max(existing, default=0) + 1
        _tenant, vendor, entity = self._make_key(tenant_id, mapping.vendor, mapping.entity_type).split(":")
        self._put_version(tenant_id, mapping.vendor, mapping.entity_type, {
            "version": version,
            "vendor": vendor,
            "entity_type": entity,
            "status": "PROPOSED",
            "proposed_by": proposed_by,
            "proposed_at": time.time(),
            "mapping": entity_mapping_to_dict(mapping),
        })
        return version

    def approve_version(self, tenant_id: str, vendor: str, entity_type: str | EntityType, version: int,
                        approver: str) -> Dict:
        versions = self._versions(tenant_id, vendor, entity_type)
        row = versions.get(version)
        if row is None:
            raise MappingGovernanceError("NOT_FOUND", f"version {version}")
        if row["status"] == "ACTIVE":
            return row
        if row["status"] != "PROPOSED":
            raise MappingGovernanceError("INVALID_STATE", row["status"])
        if approver == row["proposed_by"]:
            raise MappingGovernanceError("SAME_PERSON", "the approver must not be the proposer")
        for other in versions.values():
            if other["status"] == "ACTIVE":
                other["status"] = "SUPERSEDED"
                self._put_version(tenant_id, vendor, entity_type, other)
        row.update(status="ACTIVE", approved_by=approver, approved_at=time.time())
        self._put_version(tenant_id, vendor, entity_type, row)
        self.backend.set(self.NAMESPACE, self._make_key(tenant_id, vendor, entity_type), row["mapping"])
        return row

    def list_versions(self, tenant_id: str) -> List[Dict]:
        prefix = f"{tenant_id}:"
        rows = [r for k, r in sorted(self.backend.get_all(self.VERSIONS_NAMESPACE).items()) if k.startswith(prefix)]
        return [{k: v for k, v in r.items() if k != "mapping"} for r in rows]
