"""Unit tests for SchemaIntrospector in hrms-agentic-plugin."""

from __future__ import annotations

import pytest

from hrms_plugin.schema.introspector import (
    DiscoveredEntity,
    FieldDataType,
    IntrospectionResult,
    SchemaIntrospector,
)


class TestSchemaIntrospector:
    def test_introspect_openapi_dict(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test HRMS API", "version": "2.1.0"},
            "paths": {
                "/api/employees": {
                    "get": {"tags": ["Employee"], "summary": "List employees"},
                    "post": {"tags": ["Employee"], "summary": "Create employee"},
                }
            },
            "components": {
                "schemas": {
                    "Employee": {
                        "type": "object",
                        "required": ["id", "email"],
                        "properties": {
                            "id": {"type": "string", "format": "uuid"},
                            "name": {"type": "string"},
                            "email": {"type": "string", "format": "email"},
                            "salary": {"type": "number"},
                            "status": {"type": "string", "enum": ["ACTIVE", "TERMINATED"]},
                            "hireDate": {"type": "string", "format": "date"},
                            "createdAt": {"type": "string", "format": "date-time"},
                        },
                    }
                }
            },
        }

        result = SchemaIntrospector.introspect_openapi(spec)
        assert result.title == "Test HRMS API"
        assert result.version == "2.1.0"
        assert result.endpoints_count == 1

        emp = result.get_entity("Employee")
        assert emp is not None
        assert emp.id_field == "id"
        assert emp.read_endpoint == "GET /api/employees"
        assert emp.write_endpoint == "POST /api/employees"

        # Check fields
        assert emp.fields["id"].is_required is True
        assert emp.fields["email"].is_required is True
        assert emp.fields["name"].is_required is False
        assert emp.fields["hireDate"].data_type == FieldDataType.DATE
        assert emp.fields["createdAt"].data_type == FieldDataType.DATETIME
        assert emp.fields["salary"].data_type == FieldDataType.NUMBER
        assert emp.fields["status"].enum_values == ["ACTIVE", "TERMINATED"]

    def test_introspect_json_samples(self):
        samples = [
            {
                "emp_id": "EMP001",
                "first_name": "Saswat",
                "last_name": "Mohanty",
                "email": "saswat.mohanty@iceipts.com",
                "salary": 95000.00,
                "is_manager": True,
                "joining_date": "2024-01-10",
                "skills": ["Python", "AI", "Agentic Systems"],
                "status": "ACTIVE",
            },
            {
                "emp_id": "EMP002",
                "first_name": "Rohan",
                "last_name": "Verma",
                "email": "rohan@example.com",
                "salary": 65000.00,
                "is_manager": False,
                "joining_date": "2024-03-15",
                "skills": ["TypeScript", "React"],
                "status": "PROBATION",
            },
        ]

        entity = SchemaIntrospector.introspect_json_samples("Staff", samples)
        assert entity.name == "Staff"
        assert entity.id_field == "emp_id"

        assert entity.fields["emp_id"].data_type == FieldDataType.STRING
        assert entity.fields["salary"].data_type == FieldDataType.NUMBER
        assert entity.fields["is_manager"].data_type == FieldDataType.BOOLEAN
        assert entity.fields["joining_date"].data_type == FieldDataType.DATE
        assert entity.fields["skills"].data_type == FieldDataType.ARRAY
        assert "ACTIVE" in entity.fields["status"].enum_values
        assert "PROBATION" in entity.fields["status"].enum_values
