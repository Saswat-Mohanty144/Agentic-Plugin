"""Unit tests for MappingSynthesizer in hrms-agentic-plugin."""

from __future__ import annotations

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField, FieldDataType
from hrms_plugin.schema.synthesizer import MappingSynthesizer


class TestMappingSynthesizer:
    def test_synthesize_employee_with_compound_names(self):
        discovered = DiscoveredEntity(
            name="EmployeeModel",
            id_field="employee_id",
            fields={
                "employee_id": DiscoveredField(name="employee_id", path="employee_id", data_type=FieldDataType.STRING),
                "first_name": DiscoveredField(name="first_name", path="first_name", data_type=FieldDataType.STRING),
                "last_name": DiscoveredField(name="last_name", path="last_name", data_type=FieldDataType.STRING),
                "email": DiscoveredField(name="email", path="email", data_type=FieldDataType.STRING),
                "doj": DiscoveredField(name="doj", path="doj", data_type=FieldDataType.DATE),
                "department": DiscoveredField(name="department", path="department", data_type=FieldDataType.STRING),
                "status": DiscoveredField(
                    name="status",
                    path="status",
                    data_type=FieldDataType.STRING,
                    enum_values=["ACTIVE", "PROBATION"],
                ),
            },
        )

        mapping, report = MappingSynthesizer.synthesize(
            discovered=discovered,
            canonical_type=EntityType.EMPLOYEE,
            vendor="custom_hrms",
        )

        assert mapping.vendor == "custom_hrms"
        assert mapping.id_path == "employee_id"
        assert report.average_confidence >= 0.85

        # Compound name test
        full_name_fm = next(f for f in mapping.fields if f.target == "full_name")
        assert full_name_fm.transform == "join_names"
        assert set(full_name_fm.source) == {"first_name", "last_name"}

        # Date of joining test
        doj_fm = next(f for f in mapping.fields if f.target == "joining_date")
        assert doj_fm.source == "doj"
        assert doj_fm.transform == "date"

    def test_synthesize_leave_request(self):
        discovered = DiscoveredEntity(
            name="LeaveRequest",
            id_field="id",
            fields={
                "id": DiscoveredField(name="id", path="id", data_type=FieldDataType.STRING),
                "emp_id": DiscoveredField(name="emp_id", path="emp_id", data_type=FieldDataType.STRING),
                "leave_type": DiscoveredField(name="leave_type", path="leave_type", data_type=FieldDataType.STRING),
                "start_date": DiscoveredField(name="start_date", path="start_date", data_type=FieldDataType.DATE),
                "end_date": DiscoveredField(name="end_date", path="end_date", data_type=FieldDataType.DATE),
                "total_days": DiscoveredField(name="total_days", path="total_days", data_type=FieldDataType.NUMBER),
                "reason": DiscoveredField(name="reason", path="reason", data_type=FieldDataType.STRING),
                "status": DiscoveredField(
                    name="status",
                    path="status",
                    data_type=FieldDataType.STRING,
                    enum_values=["SUBMITTED", "APPROVED"],
                ),
            },
        )

        mapping, report = MappingSynthesizer.synthesize(
            discovered=discovered,
            canonical_type=EntityType.LEAVE_REQUEST,
            vendor="hrms_v2",
        )

        targets = {f.target for f in mapping.fields}
        assert "employee_ref" in targets
        assert "leave_type" in targets
        assert "start_date" in targets
        assert "end_date" in targets
        assert "total_days" in targets

        status_fm = next(f for f in mapping.fields if f.target == "status")
        assert status_fm.values.get("SUBMITTED") == "PENDING"
        assert status_fm.values.get("APPROVED") == "APPROVED"
