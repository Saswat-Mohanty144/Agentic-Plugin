"""Unit tests for declarative path resolution and Mapping engine in hrms-agentic-plugin."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from hrms_plugin.schema.mapping import (
    TRANSFORMS,
    EntityMapping,
    FieldMap,
    resolve_path,
)


class TestPathResolution:
    def test_simple_dot_navigation(self):
        payload = {"employee": {"profile": {"name": "Saswat"}}}
        assert resolve_path(payload, "employee.profile.name") == "Saswat"
        assert resolve_path(payload, "employee.missing") is None

    def test_array_indexing(self):
        payload = {"items": [{"id": 101}, {"id": 102}]}
        assert resolve_path(payload, "items[0].id") == 101
        assert resolve_path(payload, "items[1].id") == 102
        assert resolve_path(payload, "items[5].id") is None

    def test_array_projection(self):
        payload = {
            "education": [
                {"degree": "B.Tech", "year": 2022},
                {"degree": "M.S.", "year": 2024},
            ]
        }
        assert resolve_path(payload, "education[].degree") == ["B.Tech", "M.S."]

    def test_non_dict_traversal_returns_none(self):
        assert resolve_path("string_value", "a.b.c") is None


class TestTransforms:
    def test_strip_and_casing(self):
        assert TRANSFORMS["strip"]("  hello world  ") == "hello world"
        assert TRANSFORMS["upper"]("active") == "ACTIVE"
        assert TRANSFORMS["lower"]("DRAFT") == "draft"

    def test_decimal_conversion(self):
        assert TRANSFORMS["decimal"]("12500.75") == Decimal("12500.75")
        assert TRANSFORMS["decimal"](45000) == Decimal("45000")
        assert TRANSFORMS["decimal"](None) is None
        assert TRANSFORMS["decimal"]("invalid") is None

    def test_date_and_datetime(self):
        d = TRANSFORMS["date"]("2024-05-15")
        assert isinstance(d, date)
        assert d.year == 2024 and d.month == 5 and d.day == 15

        dt = TRANSFORMS["datetime"]("2024-05-15T10:30:00Z")
        assert isinstance(dt, datetime)
        assert dt.year == 2024

    def test_join_names(self):
        assert TRANSFORMS["join_names"](["Saswat", "Mohanty"]) == "Saswat Mohanty"
        assert TRANSFORMS["join_names"](["First", None, "Last"]) == "First Last"


class TestEntityMapping:
    def test_bidirectional_mapping(self):
        mapping = EntityMapping(
            vendor="test_hrms",
            entity_type="EMPLOYEE",
            id_path="emp_id",
            fields=(
                FieldMap(target="full_name", source=("first_name", "last_name"), transform="join_names"),
                FieldMap(target="work_email", source="email", transform="strip"),
                FieldMap(
                    target="status",
                    source="status_code",
                    transform="upper",
                    values={"1": "ACTIVE", "2": "TERMINATED"},
                ),
            ),
            writeback={"work_email": "email", "status": "status_code"},
        )

        # Inbound
        host_payload = {
            "emp_id": "EMP-900",
            "first_name": "Dev",
            "last_name": "Sharma",
            "email": " dev@example.com ",
            "status_code": "1",
        }

        canonical = mapping.to_canonical(host_payload)
        assert mapping.external_id(host_payload) == "EMP-900"
        assert canonical["full_name"] == "Dev Sharma"
        assert canonical["work_email"] == "dev@example.com"
        assert canonical["status"] == "ACTIVE"

        # Outbound Writeback
        patch = mapping.to_vendor({"work_email": "new.email@example.com", "status": "2"})
        assert patch == {"email": "new.email@example.com", "status_code": "2"}
