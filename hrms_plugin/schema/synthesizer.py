"""Mapping Synthesizer: Semantic schema alignment for host HRMS platforms.

Matches discovered host schemas against Canonical Domain Models (CanonicalCandidate,
CanonicalRequisition, CanonicalEmployee, CanonicalLeaveRequest, CanonicalPunch, etc.)
and generates deterministic, production-ready EntityMapping instances.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, FieldDataType
from hrms_plugin.schema.mapping import EntityMapping, FieldMap

__all__ = [
    "SynthesizedFieldMap",
    "SynthesisReport",
    "MappingSynthesizer",
]


class SynthesizedFieldMap(BaseModel):
    canonical_target: str
    host_source_path: str
    transform: str = "string"
    confidence: float = 1.0
    rationale: str = ""
    enum_translation: Dict[str, str] = Field(default_factory=dict)
    is_required: bool = False


class SynthesisReport(BaseModel):
    entity_type: str
    vendor: str
    id_path: str
    field_mappings: List[SynthesizedFieldMap]
    average_confidence: float = 0.0
    unmapped_canonical_fields: List[str] = Field(default_factory=list)
    unmapped_host_fields: List[str] = Field(default_factory=list)
    writeback_count: int = 0


CANONICAL_SPECIFICATIONS: Dict[str, Dict[str, Dict[str, Any]]] = {
    EntityType.REQUISITION.value: {
        "title": {
            "synonyms": ["job_title", "title", "position", "role_title", "job_name", "designation"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "department": {
            "synonyms": ["department", "dept", "department_name", "departmentId", "supervisory_organization"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "location": {
            "synonyms": ["location", "city", "office_location", "work_location", "job_location", "primary_location"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "headcount": {
            "synonyms": ["headcount", "openings", "number_of_openings", "vacancies", "positions_count"],
            "transform": "int",
            "default": 1,
            "preferred_types": [FieldDataType.INTEGER, FieldDataType.NUMBER],
        },
        "status": {
            "synonyms": ["status", "state", "requisition_status", "job_status"],
            "transform": "upper",
            "preferred_types": [FieldDataType.STRING],
            "enum_map": {
                "DRAFT": "DRAFT",
                "PENDING": "PENDING",
                "PENDING_APPROVAL": "PENDING",
                "APPROVED": "OPEN",
                "ACTIVE": "OPEN",
                "OPEN": "OPEN",
                "CLOSED": "CLOSED",
                "REJECTED": "REJECTED",
                "CANCELLED": "CANCELLED",
            },
        },
        "description": {
            "synonyms": ["description", "job_description", "jd", "job_summary", "details"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "must_have_skills": {
            "synonyms": ["skills", "required_skills", "must_have_skills", "mandatory_skills", "technologies"],
            "transform": "list",
            "preferred_types": [FieldDataType.ARRAY, FieldDataType.STRING],
        },
        "nice_to_have_skills": {
            "synonyms": ["preferred_skills", "secondary_skills", "nice_to_have_skills", "good_to_have"],
            "transform": "list",
            "preferred_types": [FieldDataType.ARRAY, FieldDataType.STRING],
        },
        "min_years_experience": {
            "synonyms": ["min_experience", "min_exp", "minimum_years_experience", "experience_min", "experience"],
            "transform": "decimal",
            "preferred_types": [FieldDataType.NUMBER, FieldDataType.INTEGER, FieldDataType.STRING],
        },
        "max_years_experience": {
            "synonyms": ["max_experience", "max_exp", "maximum_years_experience", "experience_max"],
            "transform": "decimal",
            "preferred_types": [FieldDataType.NUMBER, FieldDataType.INTEGER, FieldDataType.STRING],
        },
        "hiring_manager_ref": {
            "synonyms": ["hiring_manager", "manager", "hiring_manager_id", "created_by", "createdBy"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
    },
    EntityType.CANDIDATE.value: {
        "full_name": {
            "synonyms": ["name", "full_name", "candidate_name", "applicant_name"],
            "compound": ["first_name", "last_name"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "email": {
            "synonyms": ["email", "primary_email", "email_id", "emailId", "candidate_email", "applicant_email"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "phone": {
            "synonyms": ["phone", "mobile", "contact_number", "phone_number", "primary_phone", "mobile_number"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "resume_text": {
            "synonyms": ["resume_text", "resume_content", "parsed_resume", "cv_text"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "resume_uri": {
            "synonyms": ["resume_path", "resume_uri", "resume_url", "resume_attachment", "attachment_url", "cv_url"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "skills": {
            "synonyms": ["skills", "candidate_skills", "skill_set", "technologies"],
            "transform": "list",
            "preferred_types": [FieldDataType.ARRAY, FieldDataType.STRING],
        },
        "years_experience": {
            "synonyms": ["experience", "years_experience", "total_experience", "exp_years"],
            "transform": "decimal",
            "preferred_types": [FieldDataType.NUMBER, FieldDataType.INTEGER, FieldDataType.STRING],
        },
        "current_title": {
            "synonyms": ["current_title", "current_role", "designation", "job_title", "current_designation"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "stage": {
            "synonyms": ["stage", "status", "application_status", "candidate_stage"],
            "transform": "upper",
            "preferred_types": [FieldDataType.STRING],
            "enum_map": {
                "NEW": "APPLIED",
                "APPLIED": "APPLIED",
                "SCREENING": "SCREENING",
                "SHORTLISTED": "SCREENING",
                "INTERVIEW": "INTERVIEW",
                "SCHEDULED": "INTERVIEW",
                "OFFER": "OFFER",
                "HIRED": "HIRED",
                "REJECTED": "REJECTED",
            },
        },
        "requisition_external_id": {
            "synonyms": ["requisition_id", "job_id", "jobId", "requisitionId", "job_requisition_id"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "applied_at": {
            "synonyms": ["applied_at", "created_at", "createdAt", "application_date", "date_applied"],
            "transform": "datetime",
            "preferred_types": [FieldDataType.DATETIME, FieldDataType.DATE, FieldDataType.STRING],
        },
    },
    EntityType.EMPLOYEE.value: {
        "full_name": {
            "synonyms": ["full_name", "name", "employee_name", "emp_name"],
            "compound": ["first_name", "last_name"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "work_email": {
            "synonyms": ["work_email", "email", "office_email", "official_email", "corporate_email"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "joining_date": {
            "synonyms": ["joining_date", "date_of_joining", "doj", "hire_date", "employment_date", "start_date"],
            "transform": "date",
            "preferred_types": [FieldDataType.DATE, FieldDataType.DATETIME, FieldDataType.STRING],
        },
        "department": {
            "synonyms": ["department", "department_name", "dept", "departmentId"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "designation": {
            "synonyms": ["designation", "role", "job_title", "position"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "status": {
            "synonyms": ["status", "employment_status", "employee_status"],
            "transform": "upper",
            "preferred_types": [FieldDataType.STRING],
            "enum_map": {
                "ACTIVE": "ACTIVE",
                "PROBATION": "PROBATION",
                "NOTICE": "NOTICE_PERIOD",
                "TERMINATED": "TERMINATED",
                "RESIGNED": "RESIGNED",
                "INACTIVE": "INACTIVE",
            },
        },
    },
    EntityType.LEAVE_REQUEST.value: {
        "employee_ref": {
            "synonyms": ["employee_id", "emp_id", "employeeId", "employee_ref", "empId", "applicant_id"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "leave_type": {
            "synonyms": ["leave_type", "leave_type_id", "type", "leaveType", "category"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "start_date": {
            "synonyms": ["start_date", "from_date", "startDate", "fromDate", "leave_from"],
            "transform": "date",
            "preferred_types": [FieldDataType.DATE, FieldDataType.DATETIME, FieldDataType.STRING],
            "required": True,
        },
        "end_date": {
            "synonyms": ["end_date", "to_date", "endDate", "toDate", "leave_to"],
            "transform": "date",
            "preferred_types": [FieldDataType.DATE, FieldDataType.DATETIME, FieldDataType.STRING],
            "required": True,
        },
        "total_days": {
            "synonyms": ["total_days", "days", "duration", "leave_days", "day_count"],
            "transform": "decimal",
            "preferred_types": [FieldDataType.NUMBER, FieldDataType.INTEGER, FieldDataType.STRING],
        },
        "reason": {
            "synonyms": ["reason", "remarks", "comments", "description", "justification"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
        "status": {
            "synonyms": ["status", "leave_status", "approval_status", "state"],
            "transform": "upper",
            "preferred_types": [FieldDataType.STRING],
            "enum_map": {
                "PENDING": "PENDING",
                "SUBMITTED": "PENDING",
                "APPROVED": "APPROVED",
                "REJECTED": "REJECTED",
                "CANCELLED": "CANCELLED",
            },
        },
    },
    EntityType.PUNCH.value: {
        "employee_ref": {
            "synonyms": ["employee_id", "emp_id", "employeeId", "user_id"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
            "required": True,
        },
        "punch_time": {
            "synonyms": ["punch_time", "timestamp", "log_time", "check_time", "time", "attendance_time"],
            "transform": "datetime",
            "preferred_types": [FieldDataType.DATETIME, FieldDataType.DATE, FieldDataType.STRING],
            "required": True,
        },
        "punch_type": {
            "synonyms": ["punch_type", "type", "direction", "in_out", "log_type"],
            "transform": "upper",
            "preferred_types": [FieldDataType.STRING],
            "enum_map": {
                "IN": "IN",
                "CHECKIN": "IN",
                "OUT": "OUT",
                "CHECKOUT": "OUT",
            },
        },
        "device_id": {
            "synonyms": ["device_id", "terminal_id", "machine_id", "reader_id"],
            "transform": "strip",
            "preferred_types": [FieldDataType.STRING],
        },
    },
}


class MappingSynthesizer:
    """Synthesizes EntityMapping definitions from discovered host entity schemas."""

    @classmethod
    def synthesize(
        cls,
        discovered: DiscoveredEntity,
        canonical_type: Union[EntityType, str],
        vendor: str = "host",
    ) -> Tuple[EntityMapping, SynthesisReport]:
        entity_key = canonical_type.value if isinstance(canonical_type, EntityType) else str(canonical_type)
        spec = CANONICAL_SPECIFICATIONS.get(entity_key)
        if not spec:
            raise ValueError(
                f"Unsupported canonical entity type: {entity_key}. Available: {list(CANONICAL_SPECIFICATIONS.keys())}"
            )

        # Resolve ID field
        id_path = discovered.id_field
        if not id_path:
            for candidate in ("id", "code", f"{discovered.name.lower()}_id", f"{discovered.name.lower()}Id"):
                if candidate in discovered.fields:
                    id_path = candidate
                    break
        if not id_path:
            id_path = next(iter(discovered.fields.keys()), "id")

        field_maps: List[FieldMap] = []
        synthesized_meta: List[SynthesizedFieldMap] = []
        used_host_fields: Set[str] = set()
        writeback: Dict[str, str] = {}

        for canonical_target, field_spec in spec.items():
            matched_host_path, confidence, transform, rationale, enum_trans = cls._find_best_match(
                canonical_target, field_spec, discovered, used_host_fields
            )

            if matched_host_path:
                if isinstance(matched_host_path, tuple):
                    for p in matched_host_path:
                        used_host_fields.add(p)
                else:
                    used_host_fields.add(matched_host_path)

                fm = FieldMap(
                    target=canonical_target,
                    source=matched_host_path,
                    transform=transform,
                    default=field_spec.get("default"),
                    required=field_spec.get("required", False),
                    values=enum_trans,
                )
                field_maps.append(fm)

                source_str = (
                    " + ".join(matched_host_path) if isinstance(matched_host_path, tuple) else matched_host_path
                )
                synthesized_meta.append(
                    SynthesizedFieldMap(
                        canonical_target=canonical_target,
                        host_source_path=source_str,
                        transform=transform,
                        confidence=confidence,
                        rationale=rationale,
                        enum_translation=enum_trans,
                        is_required=field_spec.get("required", False),
                    )
                )

                if confidence >= 0.8 and isinstance(matched_host_path, str):
                    writeback[canonical_target] = matched_host_path

        unmapped_canonical = [k for k in spec.keys() if k not in [m.canonical_target for m in synthesized_meta]]
        unmapped_host = [k for k in discovered.fields.keys() if k not in used_host_fields]
        avg_confidence = (
            sum(m.confidence for m in synthesized_meta) / len(synthesized_meta) if synthesized_meta else 0.0
        )

        report = SynthesisReport(
            entity_type=entity_key,
            vendor=vendor,
            id_path=id_path,
            field_mappings=synthesized_meta,
            average_confidence=round(avg_confidence, 3),
            unmapped_canonical_fields=unmapped_canonical,
            unmapped_host_fields=unmapped_host,
            writeback_count=len(writeback),
        )

        mapping = EntityMapping(
            vendor=vendor,
            entity_type=entity_key,
            id_path=id_path,
            fields=tuple(field_maps),
            writeback=writeback,
        )

        return mapping, report

    @classmethod
    def _find_best_match(
        cls,
        target_name: str,
        spec: Dict[str, Any],
        discovered: DiscoveredEntity,
        used: Set[str],
    ) -> Tuple[Optional[Union[str, Tuple[str, ...]]], float, str, str, Dict[str, str]]:
        synonyms = spec.get("synonyms", [])
        compound = spec.get("compound")
        default_transform = spec.get("transform", "string")
        enum_map = spec.get("enum_map", {})

        # Compound Name Match (e.g. first_name + last_name -> full_name)
        if compound and len(compound) == 2:
            f1, f2 = compound[0], compound[1]
            h1 = cls._find_field_by_synonyms([f1, "first", "fname"], discovered)
            h2 = cls._find_field_by_synonyms([f2, "last", "lname"], discovered)
            if h1 and h2 and (target_name not in discovered.fields):
                return (h1, h2), 0.95, "join_names", f"Compound fields ({h1} + {h2})", {}

        # Direct or Exact Synonym Match
        for syn in synonyms:
            for host_name, field_obj in discovered.fields.items():
                if host_name in used:
                    continue
                if host_name.lower() == syn.lower():
                    conf = 1.0 if host_name.lower() == target_name.lower() else 0.95
                    enum_trans = cls._align_enums(field_obj.enum_values, enum_map)
                    return (
                        host_name,
                        conf,
                        default_transform,
                        f"Exact match on synonym '{syn}'",
                        enum_trans,
                    )

        # Normalized Token Match
        target_norm = cls._normalize_name(target_name)
        for host_name, field_obj in discovered.fields.items():
            if host_name in used:
                continue
            host_norm = cls._normalize_name(host_name)
            if host_norm == target_norm:
                enum_trans = cls._align_enums(field_obj.enum_values, enum_map)
                return host_name, 0.90, default_transform, "Normalized token equivalence", enum_trans

            for syn in synonyms:
                if host_norm == cls._normalize_name(syn):
                    enum_trans = cls._align_enums(field_obj.enum_values, enum_map)
                    return host_name, 0.85, default_transform, f"Normalized match on synonym '{syn}'", enum_trans

        # Substring / Prefix Matching
        for host_name, field_obj in discovered.fields.items():
            if host_name in used:
                continue
            host_lower = host_name.lower()
            for syn in synonyms:
                syn_lower = syn.lower()
                if syn_lower in host_lower or host_lower in syn_lower:
                    if len(syn_lower) >= 4 and len(host_lower) >= 4:
                        enum_trans = cls._align_enums(field_obj.enum_values, enum_map)
                        return host_name, 0.75, default_transform, f"Substring match on '{syn}'", enum_trans

        return None, 0.0, default_transform, "No candidate match found", {}

    @classmethod
    def _find_field_by_synonyms(cls, candidates: List[str], discovered: DiscoveredEntity) -> Optional[str]:
        for c in candidates:
            c_norm = cls._normalize_name(c)
            for host_name in discovered.fields.keys():
                if cls._normalize_name(host_name) == c_norm:
                    return host_name
        return None

    @classmethod
    def _normalize_name(cls, name: str) -> str:
        s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
        return re.sub(r"[\W_]+", "", s2.lower())

    @classmethod
    def _align_enums(cls, host_enums: List[str], canonical_enum_map: Dict[str, str]) -> Dict[str, str]:
        if not host_enums or not canonical_enum_map:
            return {}

        out = {}
        for h_val in host_enums:
            h_upper = h_val.strip().upper()
            if h_upper in canonical_enum_map:
                out[h_val] = canonical_enum_map[h_upper]
            else:
                for c_k, c_v in canonical_enum_map.items():
                    if c_k in h_upper or h_upper in c_k:
                        out[h_val] = c_v
                        break
        return out
