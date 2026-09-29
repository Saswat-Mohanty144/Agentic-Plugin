"""Unit tests for Dynamic Adaptation, Expanded Canonical Models, and DynamicVendorProfile."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hrms_plugin.connectors.profiles.dynamic import DynamicVendorProfile
from hrms_plugin.schema.canonical import (
    CanonicalExpenseClaim,
    CanonicalPayslip,
    EntityType,
    SourceRef,
)
from hrms_plugin.schema.introspector import (
    DiscoveredEntity,
    DiscoveredField,
    FieldDataType,
    IntrospectionResult,
)
from hrms_plugin.schema.synthesizer import MappingSynthesizer


class TestExpandedCanonicalModels:
    def test_canonical_payslip_decimal_precision(self):
        payslip = CanonicalPayslip(
            source_ref=SourceRef(vendor="test_vendor", entity_type=EntityType.PAYSLIP, external_id="slip-999"),
            tenant_id="tenant-123",
            employee_ref="EMP-001",
            month=3,
            year=2026,
            basic_salary=Decimal("4000.00"),
            gross_salary=Decimal("5000.50"),
            net_pay=Decimal("4200.25"),
            currency="USD",
            total_allowances=Decimal("1000.50"),
            total_deductions=Decimal("800.25"),
        )
        assert payslip.gross_salary == Decimal("5000.50")
        assert payslip.net_pay == Decimal("4200.25")
        assert isinstance(payslip.gross_salary, Decimal)
        assert isinstance(payslip.net_pay, Decimal)

    def test_canonical_expense_claim(self):
        claim = CanonicalExpenseClaim(
            source_ref=SourceRef(vendor="test_vendor", entity_type=EntityType.EXPENSE_CLAIM, external_id="exp-101"),
            tenant_id="tenant-123",
            employee_ref="EMP-001",
            claim_type="travel",
            amount=Decimal("150.75"),
            currency="USD",
            claim_date=date(2026, 3, 15),
            status="SUBMITTED",
        )
        assert claim.amount == Decimal("150.75")
        assert claim.claim_type == "travel"


class TestDynamicSynthesis:
    def test_synthesize_payslip(self):
        discovered = DiscoveredEntity(
            name="PayrollSlip",
            id_field="slip_id",
            fields={
                "slip_id": DiscoveredField(name="slip_id", path="slip_id", data_type=FieldDataType.STRING),
                "employee_code": DiscoveredField(
                    name="employee_code", path="employee_code", data_type=FieldDataType.STRING
                ),
                "month": DiscoveredField(name="month", path="month", data_type=FieldDataType.INTEGER),
                "year": DiscoveredField(name="year", path="year", data_type=FieldDataType.INTEGER),
                "basic_pay": DiscoveredField(name="basic_pay", path="basic_pay", data_type=FieldDataType.NUMBER),
                "gross_salary": DiscoveredField(
                    name="gross_salary", path="gross_salary", data_type=FieldDataType.NUMBER
                ),
                "net_salary": DiscoveredField(name="net_salary", path="net_salary", data_type=FieldDataType.NUMBER),
                "curr": DiscoveredField(name="curr", path="curr", data_type=FieldDataType.STRING),
            },
        )

        mapping, report = MappingSynthesizer.synthesize(
            discovered=discovered,
            canonical_type=EntityType.PAYSLIP,
            vendor="dynamic_payroll",
        )

        assert mapping.vendor == "dynamic_payroll"
        targets = {f.target for f in mapping.fields}
        assert "employee_ref" in targets
        assert "gross_salary" in targets
        assert "net_pay" in targets
        assert "currency" in targets

        # Check decimal transform on monetary figures
        gross_fm = next(f for f in mapping.fields if f.target == "gross_salary")
        assert gross_fm.transform == "decimal"

    def test_synthesize_custom_unmodeled_entity(self):
        """Verify that an arbitrary custom entity falls back to dynamic 1:1 synthesis gracefully."""
        discovered = DiscoveredEntity(
            name="ComplianceBadge",
            id_field="badge_uuid",
            fields={
                "badge_uuid": DiscoveredField(name="badge_uuid", path="badge_uuid", data_type=FieldDataType.STRING),
                "badge_title": DiscoveredField(name="badge_title", path="badge_title", data_type=FieldDataType.STRING),
                "issued_at": DiscoveredField(name="issued_at", path="issued_at", data_type=FieldDataType.DATE),
                "level": DiscoveredField(name="level", path="level", data_type=FieldDataType.NUMBER),
            },
        )

        # Custom entity type string
        mapping, report = MappingSynthesizer.synthesize(
            discovered=discovered,
            canonical_type="CUSTOM",
            vendor="bespoke_hrms",
        )

        assert mapping.vendor == "bespoke_hrms"
        assert mapping.id_path == "badge_uuid"
        targets = {f.target for f in mapping.fields}
        assert "badge_title" in targets
        assert "issued_at" in targets
        assert "level" in targets


class TestDynamicVendorProfile:
    def test_from_introspection(self):
        introspection = IntrospectionResult(
            title="Test HRMS",
            version="1.0.0",
            entities={
                "Employee": DiscoveredEntity(
                    name="Employee",
                    id_field="id",
                    read_endpoint="GET /api/v2/workers/{id}",
                    write_endpoint="POST /api/v2/workers",
                ),
                "LeaveRequest": DiscoveredEntity(
                    name="LeaveRequest",
                    id_field="id",
                    read_endpoint="GET /api/v2/time-off",
                    write_endpoint="POST /api/v2/time-off",
                ),
            },
        )

        profile = DynamicVendorProfile.from_introspection(introspection, vendor_name="introspected_hrms")

        # Test single worker read
        path = profile.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="W-12345")
        assert path == "/api/v2/workers/W-12345"

        # Test list workers
        list_path = profile.endpoint_for(EntityType.EMPLOYEE, "list")
        assert list_path == "/api/v2/workers"

        # Test leave create
        create_path = profile.endpoint_for(EntityType.LEAVE_REQUEST, "create")
        assert create_path == "/api/v2/time-off"

    def test_unwrap_response(self):
        profile = DynamicVendorProfile()
        payload = {"data": {"items": [{"id": "1"}, {"id": "2"}]}}
        unwrapped = profile.unwrap_response(payload, "list", EntityType.EMPLOYEE)
        assert unwrapped == [{"id": "1"}, {"id": "2"}]

    def test_register_endpoint_override(self):
        profile = DynamicVendorProfile()
        profile.register_endpoint("EMPLOYEE", "get", "/custom/emp/{id}")
        assert profile.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="99") == "/custom/emp/99"
