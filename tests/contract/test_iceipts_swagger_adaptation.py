"""Contract verification test: Introspecting and adapting real iceipts_hrms Swagger spec."""

from __future__ import annotations

from pathlib import Path

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import FieldDataType, SchemaIntrospector
from hrms_plugin.schema.prober import ShadowProber
from hrms_plugin.schema.synthesizer import MappingSynthesizer

ICEIPTS_SWAGGER_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "iceipts_hrms" / "swagger" / "swagger.yaml"
)


class TestIceiptsSwaggerAdaptation:
    @pytest.mark.skipif(not ICEIPTS_SWAGGER_PATH.exists(), reason="iceipts_hrms swagger.yaml not found")
    def test_full_roundtrip_iceipts_job_requisition(self):
        # 1. Introspect real Swagger specification
        result = SchemaIntrospector.introspect_file(ICEIPTS_SWAGGER_PATH)
        assert result.title == "Employee Management API"
        assert result.endpoints_count > 0

        req_entity = result.get_entity("JobRequisition")
        assert req_entity is not None
        assert req_entity.id_field == "id"
        assert req_entity.fields["title"].data_type == FieldDataType.STRING
        assert req_entity.fields["skills"].data_type == FieldDataType.ARRAY

        # 2. Synthesize CanonicalRequisition mapping
        mapping, report = MappingSynthesizer.synthesize(
            discovered=req_entity,
            canonical_type=EntityType.REQUISITION,
            vendor="iceipts_hrms",
        )
        assert mapping.vendor == "iceipts_hrms"
        assert report.average_confidence >= 0.85
        assert "title" in mapping.writeback

        # 3. Probe with real-world sample record
        sample_requisition = {
            "id": "a1b2c3d4-e5f6-7890-1234-56789abcdef0",
            "title": "Staff Software Engineer",
            "description": "Architect autonomous AI plugins and microservices",
            "location": "Bengaluru",
            "minSalary": 2500000,
            "maxSalary": 3500000,
            "status": "APPROVED",
            "skills": ["Python", "FastAPI", "PostgreSQL"],
            "createdBy": "usr_saswat_01",
        }

        probe_report = ShadowProber.probe(mapping, [sample_requisition])
        assert probe_report.is_valid is True
        assert probe_report.error_count == 0
        assert probe_report.quality_score >= 0.95

        # 4. Canonical Projection verification
        canonical = mapping.to_canonical(sample_requisition)
        assert canonical["title"] == "Staff Software Engineer"
        assert canonical["location"] == "Bengaluru"
        assert canonical["status"] == "OPEN"  # APPROVED was mapped to canonical OPEN
        assert canonical["hiring_manager_ref"] == "usr_saswat_01"

        # 5. Writeback Projection verification
        patch = mapping.to_vendor({"title": "Principal AI Architect", "status": "CLOSED"})
        assert patch["title"] == "Principal AI Architect"
        assert patch["status"] == "CLOSED"
