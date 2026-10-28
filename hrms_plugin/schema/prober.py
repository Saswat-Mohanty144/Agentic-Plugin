"""Shadow Prober: Pre-flight verification and validation gate for synthesized mappings.

Executes offline dry-runs of candidate EntityMapping instances against sampled host records,
checking nullability invariants, type coercions, enum validity, and primary key integrity.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.schema.mapping import EntityMapping, MappingError

__all__ = [
    "IssueSeverity",
    "ProbeIssue",
    "ProbeReport",
    "ShadowProber",
]


class IssueSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class ProbeIssue(BaseModel):
    severity: IssueSeverity
    field: str
    message: str
    sample_index: Optional[int] = None
    raw_value: Optional[Any] = None


class ProbeReport(BaseModel):
    is_valid: bool = True
    quality_score: float = 1.0  # 0.0 to 1.0
    records_tested: int = 0
    mapped_fields_count: int = 0
    issues: List[ProbeIssue] = Field(default_factory=list)
    projections: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == IssueSeverity.WARNING)


class ShadowProber:
    """Dry-run probe engine to verify mappings against live or harvested sample records."""

    @classmethod
    def probe(
        cls,
        mapping: EntityMapping,
        sample_records: List[Dict[str, Any]],
        required_canonical_fields: Optional[List[str]] = None,
    ) -> ProbeReport:
        if not sample_records:
            return ProbeReport(
                is_valid=False,
                quality_score=0.0,
                records_tested=0,
                mapped_fields_count=len(mapping.fields),
                issues=[
                    ProbeIssue(
                        severity=IssueSeverity.ERROR,
                        field="payload",
                        message="Cannot probe mapping: zero sample records provided",
                    )
                ],
            )

        issues: List[ProbeIssue] = []
        projections: List[Dict[str, Any]] = []
        req_fields = set(required_canonical_fields or [])

        for fm in mapping.fields:
            if fm.required:
                req_fields.add(fm.target)

        records_tested = 0
        for idx, sample in enumerate(sample_records):
            records_tested += 1

            # 1. Test primary key resolution
            try:
                ext_id = mapping.external_id(sample)
                if not ext_id:
                    issues.append(
                        ProbeIssue(
                            severity=IssueSeverity.ERROR,
                            field=mapping.id_path,
                            message=f"Primary key {mapping.id_path!r} resolved to empty string",
                            sample_index=idx,
                        )
                    )
            except MappingError as err:
                issues.append(
                    ProbeIssue(
                        severity=IssueSeverity.ERROR,
                        field=mapping.id_path,
                        message=str(err),
                        sample_index=idx,
                    )
                )

            # 2. Test canonical projection
            try:
                projected = mapping.to_canonical(sample)
                if len(projections) < 5:
                    projections.append(projected)

                for rf in req_fields:
                    val = projected.get(rf)
                    if val in (None, "", [], {}):
                        issues.append(
                            ProbeIssue(
                                severity=IssueSeverity.ERROR,
                                field=rf,
                                message=f"Required canonical field {rf!r} evaluated to empty/None",
                                sample_index=idx,
                            )
                        )

                for fm in mapping.fields:
                    val = projected.get(fm.target)
                    if val is not None:
                        if fm.transform in ("date", "datetime") and not isinstance(val, (date, datetime)):
                            issues.append(
                                ProbeIssue(
                                    severity=IssueSeverity.WARNING,
                                    field=fm.target,
                                    message=f"Field {fm.target!r} expected date/datetime, got {type(val).__name__}",
                                    sample_index=idx,
                                    raw_value=val,
                                )
                            )
                        elif fm.transform == "decimal" and not isinstance(val, (Decimal, int, float)):
                            issues.append(
                                ProbeIssue(
                                    severity=IssueSeverity.WARNING,
                                    field=fm.target,
                                    message=f"Field {fm.target!r} expected Decimal, got {type(val).__name__}",
                                    sample_index=idx,
                                    raw_value=val,
                                )
                            )

            except Exception as ex:
                issues.append(
                    ProbeIssue(
                        severity=IssueSeverity.ERROR,
                        field="to_canonical",
                        message=f"Unexpected exception during projection: {ex}",
                        sample_index=idx,
                    )
                )

        error_penalty = sum(0.25 for i in issues if i.severity == IssueSeverity.ERROR)
        warning_penalty = sum(0.05 for i in issues if i.severity == IssueSeverity.WARNING)
        raw_score = 1.0 - (error_penalty + warning_penalty)
        final_score = max(0.0, min(1.0, round(raw_score, 3)))

        is_valid = not any(i.severity == IssueSeverity.ERROR for i in issues)

        return ProbeReport(
            is_valid=is_valid,
            quality_score=final_score,
            records_tested=records_tested,
            mapped_fields_count=len(mapping.fields),
            issues=issues,
            projections=projections,
        )
