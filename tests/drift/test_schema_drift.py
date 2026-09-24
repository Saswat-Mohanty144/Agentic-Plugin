"""Tier 4 Tests: Schema Drift, Field Mutation Invariance & Robustness Fuzzing."""

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import SchemaIntrospector
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.schema.prober import ShadowProber
from hrms_plugin.schema.synthesizer import MappingSynthesizer


@pytest.fixture
def introspector():
    return SchemaIntrospector()


@pytest.fixture
def synthesizer():
    return MappingSynthesizer()


@pytest.fixture
def prober():
    return ShadowProber()


def test_schema_drift_field_renaming_resilience(introspector, synthesizer):
    """Test that the synthesizer can still align mutated field names (e.g. 'emp_fname' -> 'firstName')."""
    mutated_samples = [
        {
            "emp_id": "E100",
            "emp_fname": "John",
            "emp_lname": "Doe",
            "mail_addr": "john@corp.com",
            "join_dt": "2023-01-15",
        },
        {
            "emp_id": "E101",
            "emp_fname": "Jane",
            "emp_lname": "Smith",
            "mail_addr": "jane@corp.com",
            "join_dt": "2023-02-20",
        },
    ]

    discovered = introspector.introspect_json_samples("Employee", mutated_samples)

    mapping, report = synthesizer.synthesize(
        discovered=discovered,
        canonical_type=EntityType.EMPLOYEE,
    )

    # Verify key canonical targets are correctly mapped despite mutated names
    mapped_targets = {m.target: m.source for m in mapping.fields}
    assert "work_email" in mapped_targets
    assert mapped_targets["work_email"] == "mail_addr"
    assert "joining_date" in mapped_targets
    assert mapped_targets["joining_date"] == "join_dt"


def test_prober_detects_schema_drift_nullability_failure(prober):
    """Test that the prober flags severe schema drift when a previously reliable field becomes 100% null."""
    # Mapping expects 'official_email'
    mapping = EntityMapping(
        vendor="custom",
        entity_type=EntityType.EMPLOYEE.value,
        id_path="id",
        fields=(
            FieldMap(target="email", source="official_email", required=True),
        ),
    )

    # Mutated payload where official_email is missing or null
    drifted_records = [
        {"id": "1", "official_email": None},
        {"id": "2", "official_email": None},
        {"id": "3", "official_email": None},
    ]

    report = prober.probe(mapping, drifted_records)
    assert report.is_valid is False
    assert report.error_count >= 1
    assert any("required field 'email' is empty" in issue.message for issue in report.issues)


def test_type_coercion_string_to_numeric_invariance():
    """Test that stringified numeric fields (e.g. '"1200000.00"') parse deterministically."""
    from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent

    payroll = StatutoryPayrollAgent()
    # String input
    res_str = payroll.structure_india_salary(annual_ctc="1200000.00")
    # Float input
    res_flt = payroll.structure_india_salary(annual_ctc=1200000.0)

    assert res_str.monthly_ctc == res_flt.monthly_ctc == 100000.0
    assert res_str.gross_salary == res_flt.gross_salary
    assert res_str.net_take_home == res_flt.net_take_home
