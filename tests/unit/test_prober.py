"""Unit tests for ShadowProber in hrms-agentic-plugin."""

from __future__ import annotations

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField, FieldDataType
from hrms_plugin.schema.prober import IssueSeverity, ShadowProber
from hrms_plugin.schema.synthesizer import MappingSynthesizer


class TestShadowProber:
    def test_probe_valid_sample_records(self):
        discovered = DiscoveredEntity(
            name="JobRequisition",
            id_field="id",
            fields={
                "id": DiscoveredField(name="id", path="id", data_type=FieldDataType.STRING),
                "title": DiscoveredField(name="title", path="title", data_type=FieldDataType.STRING),
                "description": DiscoveredField(name="description", path="description", data_type=FieldDataType.STRING),
                "status": DiscoveredField(
                    name="status",
                    path="status",
                    data_type=FieldDataType.STRING,
                    enum_values=["DRAFT", "APPROVED"],
                ),
            },
        )
        mapping, _ = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION)

        samples = [
            {"id": "REQ-1", "title": "AI Engineer", "description": "Build LLM systems", "status": "APPROVED"},
            {"id": "REQ-2", "title": "DevOps Lead", "description": "Manage K8s clusters", "status": "DRAFT"},
        ]

        report = ShadowProber.probe(mapping, samples)
        assert report.is_valid is True
        assert report.error_count == 0
        assert report.quality_score >= 0.95
        assert len(report.projections) == 2
        assert report.projections[0]["title"] == "AI Engineer"
        assert report.projections[0]["status"] == "OPEN"

    def test_probe_catches_missing_primary_key(self):
        discovered = DiscoveredEntity(
            name="BadEntity",
            id_field="missing_key",
            fields={"title": DiscoveredField(name="title", path="title", data_type=FieldDataType.STRING)},
        )
        mapping, _ = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION)

        report = ShadowProber.probe(mapping, [{"title": "Software Engineer"}])
        assert report.is_valid is False
        assert report.error_count > 0
        assert any(i.field == "missing_key" for i in report.issues)

    def test_probe_empty_samples(self):
        discovered = DiscoveredEntity(name="Empty", id_field="id")
        mapping, _ = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION)
        report = ShadowProber.probe(mapping, [])
        assert report.is_valid is False
        assert report.quality_score == 0.0
