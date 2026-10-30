"""Persistent storage and registry for compiled EntityMapping definitions."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from hrms_plugin.governance.classification_rules import DATA_CLASSES, SENSITIVE_CLASSES
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
    def __init__(
        self,
        code: str,
        detail: str = "",
        fields: Optional[List[str]] = None,
        problems: Optional[List[str]] = None,
    ):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.fields = fields or []
        self.problems = problems or []


def classify_fields(
    mapping: Dict[str, Any],
    given: Optional[Dict[str, Any]],
    coverage: Optional[Dict[str, Any]] = None,
    accept_suggested_classification: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """Q3/K1.5: every mapped field gets a data class; unclassified fields take suggestion when accepted, else PII."""
    targets = [f["target"] for f in mapping.get("fields", [])]
    given = given or {}
    unknown = sorted(set(given) - set(targets))
    if unknown:
        raise MappingGovernanceError("CLASSIFICATION_UNKNOWN_FIELD", ",".join(unknown))

    cov_fields = (coverage or {}).get("fields", {}) if isinstance(coverage, dict) else {}
    result: Dict[str, Dict[str, Any]] = {}
    origins: Dict[str, str] = {}

    for target in targets:
        if target in given:
            entry = given[target]
            if (not isinstance(entry, dict) or set(entry) != {"data_class", "llm_visible"}
                    or entry["data_class"] not in DATA_CLASSES or not isinstance(entry["llm_visible"], bool)):
                raise MappingGovernanceError("INVALID_CLASSIFICATION", target)
            if entry["llm_visible"] and entry["data_class"] in SENSITIVE_CLASSES:
                raise MappingGovernanceError("SENSITIVE_FIELD_LLM_VISIBLE", target)
            result[target] = {"data_class": entry["data_class"], "llm_visible": entry["llm_visible"]}
            origins[target] = "MANUAL"
        elif accept_suggested_classification:
            sug = (
                cov_fields.get(target, {}).get("suggested_classification")
                if isinstance(cov_fields.get(target), dict)
                else None
            )
            if (
                isinstance(sug, dict)
                and sug.get("data_class") in DATA_CLASSES
                and isinstance(sug.get("llm_visible"), bool)
            ):
                if sug["llm_visible"] and sug["data_class"] in SENSITIVE_CLASSES:
                    raise MappingGovernanceError("SENSITIVE_FIELD_LLM_VISIBLE", target)
                result[target] = {"data_class": sug["data_class"], "llm_visible": sug["llm_visible"]}
                rule_id = sug.get("rule_id", "UNKNOWN")
                origins[target] = f"SUGGESTED:{rule_id}"
            else:
                result[target] = {"data_class": "PII", "llm_visible": False}
                origins[target] = "DEFAULT"
        else:
            result[target] = {"data_class": "PII", "llm_visible": False}
            origins[target] = "DEFAULT"

    return result, origins


class MappingStore:
    """Persistent registry for tenant-specific compiled schema mappings."""

    NAMESPACE = "entity_mappings"

    def __init__(self, backend: Optional[BaseKeyValueStore] = None) -> None:
        self.backend: BaseKeyValueStore = backend or SqliteKeyValueStore()
        self._lock = threading.RLock()

    @staticmethod
    def _make_key(tenant_id: str, vendor: str, entity_type: str | EntityType) -> str:
        ent = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type).upper()
        if any(":" in part for part in (tenant_id, vendor, ent)):
            raise MappingGovernanceError("INVALID_KEY", "tenant, vendor and entity type must not contain ':'")
        return f"{tenant_id}:{vendor.lower()}:{ent}"

    def _activate(
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

    @classmethod
    def _default_coverage(cls, mapping: EntityMapping) -> Dict[str, Any]:
        from hrms_plugin.governance.classification_rules import suggest_classification
        from hrms_plugin.schema.synthesizer import CANONICAL_SPECIFICATIONS

        spec = CANONICAL_SPECIFICATIONS.get(mapping.entity_type, {})
        fields_cov: Dict[str, Dict[str, Any]] = {}
        mapped_targets = {f.target: f for f in mapping.fields}
        all_targets = list(spec.keys())
        for t in mapped_targets:
            if t not in all_targets:
                all_targets.append(t)

        for target in all_targets:
            field_spec = spec.get(target, {})
            sug = suggest_classification(target, mapping.entity_type)
            if target in mapped_targets:
                fm = mapped_targets[target]
                source_str = " + ".join(fm.source) if isinstance(fm.source, (list, tuple)) else (fm.source or "")
                fields_cov[target] = {
                    "status": "NEEDS_REVIEW",
                    "source": source_str,
                    "confidence": None,
                    "origin": None,
                    "rationale": "proposed without a synthesis report",
                    "required": bool(fm.required or field_spec.get("required", False)),
                    "accepted_by": None,
                    "suggested_classification": sug,
                }
            else:
                fields_cov[target] = {
                    "status": "MISSING",
                    "source": None,
                    "confidence": None,
                    "origin": None,
                    "rationale": "",
                    "required": bool(field_spec.get("required", False)),
                    "accepted_by": None,
                    "suggested_classification": sug,
                }
        counts = {
            "matched": sum(1 for f in fields_cov.values() if f["status"] == "MATCHED"),
            "needs_review": sum(1 for f in fields_cov.values() if f["status"] == "NEEDS_REVIEW"),
            "missing": sum(1 for f in fields_cov.values() if f["status"] == "MISSING"),
            "unused": 0,
        }
        return {
            "fields": fields_cov,
            "unused_host_fields": [],
            "counts": counts,
        }

    def propose_mapping(self, tenant_id: str, mapping: EntityMapping, proposed_by: str,
                        probe: Optional[Dict[str, Any]] = None,
                        coverage: Optional[Dict[str, Any]] = None) -> int:
        with self._lock:  # version allocation must be atomic within the process (agy G-F7)
            return self._propose_locked(tenant_id, mapping, proposed_by, probe, coverage)

    def _propose_locked(self, tenant_id: str, mapping: EntityMapping, proposed_by: str,
                        probe: Optional[Dict[str, Any]] = None,
                        coverage: Optional[Dict[str, Any]] = None) -> int:
        existing = self._versions(tenant_id, mapping.vendor, mapping.entity_type)
        version = max(existing, default=0) + 1
        _tenant, vendor, entity = self._make_key(tenant_id, mapping.vendor, mapping.entity_type).split(":")
        cov = coverage if coverage is not None else self._default_coverage(mapping)
        row: Dict[str, Any] = {
            "version": version,
            "vendor": vendor,
            "entity_type": entity,
            "status": "PROPOSED",
            "proposed_by": proposed_by,
            "proposed_at": time.time(),
            "mapping": entity_mapping_to_dict(mapping),
            "coverage": cov,
        }
        if probe is not None:
            row["probe"] = probe
        self._put_version(tenant_id, mapping.vendor, mapping.entity_type, row)
        return version

    def approve_version(self, tenant_id: str, vendor: str, entity_type: str | EntityType, version: int,
                        approver: str, classification: Optional[Dict[str, Any]] = None,
                        accept_suggested_classification: bool = False) -> Dict:
        with self._lock:
            return self._approve_locked(
                tenant_id, vendor, entity_type, version, approver, classification,
                accept_suggested_classification=accept_suggested_classification,
            )

    def _approve_locked(self, tenant_id: str, vendor: str, entity_type: str | EntityType, version: int,
                        approver: str, classification: Optional[Dict[str, Any]] = None,
                        accept_suggested_classification: bool = False) -> Dict:
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
        if row.get("probe") and row["probe"].get("is_valid") is False:
            raise MappingGovernanceError("PROBE_FAILED", "stored probe is invalid")
        needs_review = [
            target
            for target, info in sorted(row.get("coverage", {}).get("fields", {}).items())
            if isinstance(info, dict) and info.get("status") == "NEEDS_REVIEW"
        ]
        if needs_review:
            raise MappingGovernanceError(
                "REVIEW_REQUIRED",
                detail=f"needs review on fields: {','.join(needs_review)}",
                fields=needs_review,
            )
        classified, origins = classify_fields(
            row["mapping"],
            classification,
            coverage=row.get("coverage"),
            accept_suggested_classification=accept_suggested_classification,
        )
        for other in versions.values():
            if other["status"] == "ACTIVE":
                other["status"] = "SUPERSEDED"
                self._put_version(tenant_id, vendor, entity_type, other)
        row.update(
            status="ACTIVE",
            approved_by=approver,
            approved_at=time.time(),
            classification=classified,
            classification_origin=origins,
        )
        self._put_version(tenant_id, vendor, entity_type, row)
        self.backend.set(self.NAMESPACE, self._make_key(tenant_id, vendor, entity_type), row["mapping"])
        return row

    def reject_version(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
        version: int,
        rejected_by: str,
        reason: str,
    ) -> Dict[str, Any]:
        with self._lock:
            return self._reject_locked(tenant_id, vendor, entity_type, version, rejected_by, reason)

    def _reject_locked(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
        version: int,
        rejected_by: str,
        reason: str,
    ) -> Dict[str, Any]:
        versions = self._versions(tenant_id, vendor, entity_type)
        row = versions.get(version)
        if row is None:
            raise MappingGovernanceError("NOT_FOUND", f"version {version}")
        if row["status"] != "PROPOSED":
            raise MappingGovernanceError("INVALID_STATE", row["status"])
        row.update(
            status="REJECTED",
            reason=reason,
            rejected_by=rejected_by,
            rejected_at=time.time(),
        )
        self._put_version(tenant_id, vendor, entity_type, row)
        return row

    def review_version(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
        version: int,
        reviewer: str,
        actions: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock:
            return self._review_locked(tenant_id, vendor, entity_type, version, reviewer, actions)

    def _review_locked(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
        version: int,
        reviewer: str,
        actions: Dict[str, Any],
    ) -> Dict[str, Any]:
        from hrms_plugin.schema.mapping import TRANSFORMS
        from hrms_plugin.schema.synthesizer import CANONICAL_SPECIFICATIONS

        versions = self._versions(tenant_id, vendor, entity_type)
        old_row = versions.get(version)
        if old_row is None:
            raise MappingGovernanceError("NOT_FOUND", f"version {version}")
        if old_row["status"] != "PROPOSED":
            raise MappingGovernanceError("INVALID_STATE", old_row["status"])

        accept_targets = list(actions.get("accept") or [])
        reject_targets = list(actions.get("reject") or [])
        manual_dict = dict(actions.get("manual") or {})
        manual_targets = list(manual_dict.keys())

        ent_str = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type).upper()
        spec = CANONICAL_SPECIFICATIONS.get(ent_str, {})
        old_cov = old_row.get("coverage") or self._default_coverage(entity_mapping_from_dict(old_row["mapping"]))
        old_cov_fields = dict(old_cov.get("fields", {}))

        known_targets = set(spec.keys()) | set(old_cov_fields.keys())
        problems: List[str] = []

        # 1. Target in more than one list
        counts_by_target: Dict[str, int] = {}
        for t in accept_targets:
            counts_by_target[t] = counts_by_target.get(t, 0) + 1
        for t in reject_targets:
            counts_by_target[t] = counts_by_target.get(t, 0) + 1
        for t in manual_targets:
            counts_by_target[t] = counts_by_target.get(t, 0) + 1
        for t, c in sorted(counts_by_target.items()):
            if c > 1:
                problems.append(f"Target appears in multiple lists: {t}")

        # 2. Unknown target
        all_mentioned = set(counts_by_target.keys())
        for t in sorted(all_mentioned):
            if t not in known_targets:
                problems.append(f"Unknown target: {t}")

        # 3. Target not currently mapped in accept
        old_mapping = entity_mapping_from_dict(old_row["mapping"])
        mapped_targets = {f.target for f in old_mapping.fields}
        for t in sorted(accept_targets):
            if t in known_targets and t not in mapped_targets:
                problems.append(f"Target not currently mapped cannot be accepted: {t}")

        # 4. Invalid transform in manual
        for t, m_cfg in sorted(manual_dict.items()):
            if isinstance(m_cfg, dict):
                tr = m_cfg.get("transform")
                if tr is not None and tr not in TRANSFORMS:
                    problems.append(f"Invalid transform for target {t}: {tr}")

        if problems:
            raise MappingGovernanceError("REVIEW_INVALID", detail="; ".join(problems), problems=problems)

        # Allocate new version number
        new_version = max(versions, default=0) + 1

        # Mark old version REVISED with revised_into
        old_row["status"] = "REVISED"
        old_row["revised_into"] = new_version
        self._put_version(tenant_id, vendor, entity_type, old_row)

        # Build new EntityMapping
        new_field_maps: Dict[str, FieldMap] = {f.target: f for f in old_mapping.fields}
        for t in reject_targets:
            new_field_maps.pop(t, None)

        for t, m_cfg in manual_dict.items():
            field_spec = spec.get(t, {})
            source = m_cfg.get("source") if isinstance(m_cfg, dict) else str(m_cfg)
            transform = m_cfg.get("transform") or "string" if isinstance(m_cfg, dict) else "string"
            values = dict(m_cfg.get("values") or {}) if isinstance(m_cfg, dict) else {}
            new_field_maps[t] = FieldMap(
                target=t,
                source=source,
                transform=transform,
                default=field_spec.get("default"),
                required=field_spec.get("required", False),
                values=values,
            )

        new_writeback = dict(old_mapping.writeback or {})
        for t in reject_targets:
            new_writeback.pop(t, None)

        new_mapping = EntityMapping(
            vendor=old_mapping.vendor,
            entity_type=old_mapping.entity_type,
            id_path=old_mapping.id_path,
            version_path=old_mapping.version_path,
            collection_path=old_mapping.collection_path,
            writeback=new_writeback,
            fields=tuple(new_field_maps.values()),
        )

        # Build new coverage
        new_cov_fields: Dict[str, Dict[str, Any]] = {}
        all_final_targets = sorted(known_targets | set(new_field_maps.keys()))

        from hrms_plugin.governance.classification_rules import suggest_classification

        for t in all_final_targets:
            field_spec = spec.get(t, {})
            if t in reject_targets:
                new_cov_fields[t] = {
                    "status": "MISSING",
                    "source": None,
                    "confidence": None,
                    "origin": None,
                    "rationale": "Rejected in review",
                    "required": field_spec.get("required", False),
                    "accepted_by": None,
                }
            elif t in manual_dict:
                m_cfg = manual_dict[t]
                src = m_cfg.get("source") if isinstance(m_cfg, dict) else str(m_cfg)
                new_cov_fields[t] = {
                    "status": "MATCHED",
                    "source": src,
                    "confidence": None,
                    "origin": "MANUAL",
                    "rationale": "Manual configuration",
                    "required": field_spec.get("required", False),
                    "accepted_by": None,
                }
            elif t in accept_targets:
                old_f = old_cov_fields.get(t, {})
                new_cov_fields[t] = {
                    "status": "MATCHED",
                    "source": old_f.get("source"),
                    "confidence": old_f.get("confidence"),
                    "origin": old_f.get("origin"),
                    "rationale": old_f.get("rationale", ""),
                    "required": old_f.get("required", False),
                    "accepted_by": reviewer,
                }
            elif t in old_cov_fields:
                new_cov_fields[t] = dict(old_cov_fields[t])
            else:
                new_cov_fields[t] = {
                    "status": "MISSING",
                    "source": None,
                    "confidence": None,
                    "origin": None,
                    "rationale": "",
                    "required": field_spec.get("required", False),
                    "accepted_by": None,
                }

            old_f = old_cov_fields.get(t, {})
            old_sug = old_f.get("suggested_classification")
            old_src = old_f.get("source")
            new_src = new_cov_fields[t].get("source")
            if (
                isinstance(old_sug, dict)
                and old_sug.get("rule_id") == "VALUE_CHECK"
                and new_src is not None
                and new_src == old_src
            ):
                new_cov_fields[t]["suggested_classification"] = old_sug
            else:
                new_cov_fields[t]["suggested_classification"] = suggest_classification(t, ent_str)

        # Track unused host fields
        all_host_fields = set(old_cov.get("unused_host_fields", []))
        for f in old_mapping.fields:
            if isinstance(f.source, str):
                all_host_fields.add(f.source)
            elif isinstance(f.source, (list, tuple)):
                all_host_fields.update(f.source)
        for m_cfg in manual_dict.values():
            if isinstance(m_cfg, dict) and "source" in m_cfg:
                all_host_fields.add(m_cfg["source"])

        new_used_sources = set()
        for f in new_mapping.fields:
            if isinstance(f.source, str):
                new_used_sources.add(f.source)
            elif isinstance(f.source, (list, tuple)):
                new_used_sources.update(f.source)

        new_unused = sorted(all_host_fields - new_used_sources)

        new_counts = {
            "matched": sum(1 for f in new_cov_fields.values() if f["status"] == "MATCHED"),
            "needs_review": sum(1 for f in new_cov_fields.values() if f["status"] == "NEEDS_REVIEW"),
            "missing": sum(1 for f in new_cov_fields.values() if f["status"] == "MISSING"),
            "unused": len(new_unused),
        }
        new_cov = {
            "fields": new_cov_fields,
            "unused_host_fields": new_unused,
            "counts": new_counts,
        }

        # The probe (if stored) is not carried to the revision (it no longer describes the mapping)
        new_row: Dict[str, Any] = {
            "version": new_version,
            "vendor": vendor,
            "entity_type": ent_str,
            "status": "PROPOSED",
            "proposed_by": reviewer,
            "proposed_at": time.time(),
            "mapping": entity_mapping_to_dict(new_mapping),
            "coverage": new_cov,
        }
        self._put_version(tenant_id, vendor, entity_type, new_row)
        return new_row

    def active_version(self, tenant_id: str, vendor: str,
                       entity_type: str | EntityType) -> Optional[Tuple[int, EntityMapping]]:
        """The approved mapping in force and its version; proposals never count (QC)."""
        active = [r for r in self._versions(tenant_id, vendor, entity_type).values() if r["status"] == "ACTIVE"]
        if not active:
            return None
        row = max(active, key=lambda r: r["version"])
        return row["version"], entity_mapping_from_dict(row["mapping"])

    def active_classification(
        self,
        tenant_id: str,
        vendor: str,
        entity_type: str | EntityType,
    ) -> Optional[Dict[str, Any]]:
        """Return the ACTIVE version's field classification dict, or None if no active version."""
        try:
            active = [r for r in self._versions(tenant_id, vendor, entity_type).values() if r.get("status") == "ACTIVE"]
        except Exception:
            return None
        if not active:
            return None
        row = max(active, key=lambda r: r["version"])
        classification = row.get("classification")
        return dict(classification) if isinstance(classification, dict) else None

    def list_versions(self, tenant_id: str) -> List[Dict]:
        prefix = f"{tenant_id}:"
        rows = [r for k, r in sorted(self.backend.get_all(self.VERSIONS_NAMESPACE).items()) if k.startswith(prefix)]
        return [{k: v for k, v in r.items() if k != "mapping"} for r in rows]
