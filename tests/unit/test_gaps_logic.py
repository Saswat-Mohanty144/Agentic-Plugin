"""Gap-closing tests for schema logic, agents, tools, MCP, PII security, and HTTP flows.

Strict adversarial QA test suite closing uncovered branches across piece A-I.
All tests use mock transports / scripted seams with zero real network, zero real LLM,
and zero email addresses in fixtures or assertions.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, List

import pytest

from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.onboarding import (
    DocumentVerificationStatus,
    Jurisdiction,
    OfferStatus,
    OnboardingAgent,
)
from hrms_plugin.agents.orchestrator import AgenticOrchestrator, _collect_from_output, _RunState
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent, _d
from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry, ToolDefinition
from hrms_plugin.schema.canonical import (
    CanonicalCandidate,
    CanonicalEmployee,
    EntityType,
    SourceRef,
)
from hrms_plugin.schema.drift import DriftSeverity, SchemaDriftInterceptor
from hrms_plugin.schema.introspector import (
    DiscoveredEntity,
    DiscoveredField,
    FieldDataType,
    IntrospectionResult,
    SchemaIntrospector,
)
from hrms_plugin.schema.mapping import (
    TRANSFORMS,
    EntityMapping,
    FieldMap,
    MappingError,
    resolve_path,
)
from hrms_plugin.schema.prober import IssueSeverity, ShadowProber
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.security.pii import (
    PiiSession,
    _iban_matches,
    _indian_grouping,
    money_variants,
)
from hrms_plugin.security.rbac import AuthContext, Permission, UserRole
from hrms_plugin.server.mcp_server import HrmsMcpServer
from tests.support.scripted_llm import final, tool_call

ADMIN_AUTH = AuthContext(user_id="usr-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
HR_MGR_AUTH = AuthContext(user_id="usr-hr", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
EMPLOYEE_AUTH = AuthContext(user_id="usr-emp", tenant_id="acme", roles=frozenset({UserRole.EMPLOYEE}))


# ==============================================================================
# SECTION 1: SCHEMA MAPPING & PATH RESOLUTION
# ==============================================================================


@pytest.mark.parametrize(
    "path,expected",
    [
        ("", None),
        ("missing", None),
        ("user.missing", None),
        ("items[0]", "first"),
        ("items[1]", "second"),
        ("items[2]", None),
        ("[0]", "direct"),
        ("matrix[0][0]", None),  # Non-dict matrix item traversal returns None
        ("nested.items[].id", [101, 102]),
        ("nested.items[].name", ["Alpha", "Beta"]),
        ("nested.items[]", [{"id": 101, "name": "Alpha"}, {"id": 102, "name": "Beta", "skip": ""}]),
        ("nested.empty_list[]", []),
        ("nested.not_a_list[0]", None),
    ],
)
def test_resolve_path_boundaries(path: str, expected: Any):
    payload = {
        "user": {"name": "Test User"},
        "items": ["first", "second"],
        "direct": ["direct", "other"],
        "matrix": [["cell_00", "cell_01"]],
        "nested": {
            "items": [{"id": 101, "name": "Alpha"}, {"id": 102, "name": "Beta", "skip": ""}],
            "empty_list": [],
            "not_a_list": "string_scalar",
        },
    }
    if path == "[0]":
        assert resolve_path(["direct", "other"], path) == "direct"
    else:
        assert resolve_path(payload, path) == expected


def test_resolve_path_empty_and_none_handling():
    assert resolve_path(None, "user.name") is None
    assert resolve_path({}, "") is None
    assert resolve_path({"a": None}, "a.b") is None


@pytest.mark.parametrize(
    "raw_value,expected_date",
    [
        (datetime(2026, 10, 29, 12, 0, 0), date(2026, 10, 29)),
        (date(2026, 10, 29), date(2026, 10, 29)),
        ("2026-10-29T14:30:00Z", date(2026, 10, 29)),
        ("2026-10-29 14:30:00", date(2026, 10, 29)),
        ("2026-10-29", date(2026, 10, 29)),
        ("29/10/2026", date(2026, 10, 29)),
        ("10/29/2026", date(2026, 10, 29)),
        ("invalid-date-string", None),
        ("", None),
        (None, None),
    ],
)
def test_mapping_transforms_date_and_datetime(raw_value: Any, expected_date: Any):
    to_date_fn = TRANSFORMS["date"]
    to_datetime_fn = TRANSFORMS["datetime"]
    if expected_date is None:
        assert to_date_fn(raw_value) is None
        assert to_datetime_fn(raw_value) is None
    else:
        parsed_d = to_date_fn(raw_value)
        parsed_dt = to_datetime_fn(raw_value)
        assert parsed_d == expected_date
        assert parsed_dt is not None and parsed_dt.date() == expected_date


@pytest.mark.parametrize(
    "raw_value,expected_list",
    [
        (None, []),
        ("", []),
        ("Engineering, Product | Operations; Support", ["Engineering", "Product", "Operations", "Support"]),
        ([{"name": "DevOps"}, {"title": "QA"}, {"ignored": None}], ["DevOps", "QA"]),
        ([1, 2, 3], ["1", "2", "3"]),
        (42, ["42"]),
    ],
)
def test_mapping_transforms_list(raw_value: Any, expected_list: List[str]):
    assert TRANSFORMS["list"](raw_value) == expected_list


@pytest.mark.parametrize(
    "raw_value,expected_joined",
    [
        (["Tariq", "Mansoor"], "Tariq Mansoor"),
        (["   ", "Ali"], "Ali"),
        ([], None),
        ("Solo Name", "Solo Name"),
        ("   ", None),
        (None, None),
    ],
)
def test_mapping_transforms_join_names(raw_value: Any, expected_joined: Any):
    assert TRANSFORMS["join_names"](raw_value) == expected_joined


def test_field_map_unknown_transform_raises_mapping_error():
    fm = FieldMap(target="test_field", source="src", transform="non_existent_transform")
    with pytest.raises(MappingError, match="unknown transform"):
        fm.apply({"src": "val"})


def test_field_map_required_and_defaults():
    fm_req = FieldMap(target="req_field", source="missing_path", required=True)
    with pytest.raises(MappingError, match="required field 'req_field' is empty"):
        fm_req.apply({"other": "val"})

    fm_def = FieldMap(target="opt_field", source="missing_path", default="DEFAULT_VAL")
    assert fm_def.apply({"other": "val"}) == "DEFAULT_VAL"

    fm_enum = FieldMap(target="status", source="raw_status", values={"ACTIVE": "OPEN", "INACTIVE": "CLOSED"})
    assert fm_enum.apply({"raw_status": "active"}) == "OPEN"


def test_entity_mapping_external_id_and_version():
    mapping = EntityMapping(
        vendor="test_vendor",
        entity_type="EMPLOYEE",
        id_path="data.user_id",
        version_path="meta.etag",
    )
    assert mapping.external_id({"data": {"user_id": "EMP-42"}}) == "EMP-42"
    with pytest.raises(MappingError, match="no id at path 'data.user_id'"):
        mapping.external_id({"data": {}})

    assert mapping.version({"meta": {"etag": "v123"}}) == "v123"
    assert mapping.version({"meta": {}}) is None

    no_ver_mapping = EntityMapping(vendor="v", entity_type="e", id_path="id")
    assert no_ver_mapping.version({"id": "1"}) is None


def test_entity_mapping_records_envelope_extraction():
    mapping_coll = EntityMapping(
        vendor="v",
        entity_type="e",
        id_path="id",
        collection_path="data.pageData",
    )
    assert mapping_coll.records({"data": {"pageData": [{"id": 1}, {"id": 2}]}}) == [{"id": 1}, {"id": 2}]
    assert mapping_coll.records({"data": {"pageData": None}}) == []
    assert mapping_coll.records({"data": {"pageData": {"id": 1}}}) == [{"id": 1}]

    mapping_direct = EntityMapping(vendor="v", entity_type="e", id_path="id")
    assert mapping_direct.records([{"id": 1}]) == [{"id": 1}]
    assert mapping_direct.records({"id": 1}) == [{"id": 1}]
    assert mapping_direct.records(None) == []


def test_entity_mapping_to_vendor_writeback():
    no_wb = EntityMapping(vendor="v", entity_type="e", id_path="id")
    with pytest.raises(MappingError, match="is read-only: no writeback mapping"):
        no_wb.to_vendor({"salary": 50000})

    wb_mapping = EntityMapping(
        vendor="v",
        entity_type="e",
        id_path="id",
        writeback={
            "salary": "employee.compensation.base_amount",
            "effective_date": "profile.history.effective_from",
            "full_name": "personal.display_name",
            "missing_can_field": "some.path",
        },
    )
    canonical_payload = {
        "salary": Decimal("75000.50"),
        "effective_date": date(2026, 11, 1),
        "full_name": "Amina Al-Falasi",
    }
    out = wb_mapping.to_vendor(canonical_payload)
    assert out == {
        "employee": {"compensation": {"base_amount": "75000.50"}},
        "profile": {"history": {"effective_from": "2026-11-01"}},
        "personal": {"display_name": "Amina Al-Falasi"},
    }


# ==============================================================================
# SECTION 2: CANONICAL MODELS & SCHEMA DRIFT
# ==============================================================================


def test_canonical_source_ref_str():
    ref = SourceRef(vendor="iceipts", entity_type=EntityType.REQUISITION, external_id="REQ-888")
    assert str(ref) == "iceipts:REQUISITION:REQ-888"


def test_canonical_candidate_pii_literals_sanitised():
    ref = SourceRef(vendor="iceipts", entity_type=EntityType.CANDIDATE, external_id="CAND-1")
    cand = CanonicalCandidate(
        source_ref=ref,
        tenant_id="acme",
        full_name="Fatima Al-Zahra",
        phone="+971501112233",
    )
    literals = cand.pii_literals()
    assert "Fatima" in literals
    assert "Al-Zahra" in literals
    assert "+971501112233" in literals


def test_canonical_employee_pii_literals_sanitised():
    ref = SourceRef(vendor="frappe", entity_type=EntityType.EMPLOYEE, external_id="EMP-1")
    emp = CanonicalEmployee(
        source_ref=ref,
        tenant_id="acme",
        full_name="Sanjay Sharma",
        phone="+919876543210",
    )
    literals = emp.pii_literals()
    assert "Sanjay" in literals
    assert "Sharma" in literals
    assert "+919876543210" in literals


def test_schema_drift_inspect_response_and_metadata_filtering():
    detector = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="frappe",
        entity_type="EMPLOYEE",
        id_path="name",
        fields=(
            FieldMap(target="first_name", source=("first_name", "fname")),
            FieldMap(target="status", source="employment_status"),
        ),
    )
    resp = {
        "name": "EMP-001",
        "first_name": "Aditi",
        "employment_status": "Active",
        "custom_tax_code": "TAX-99",  # Unmapped new field
        "_internal_state": "syncing",  # Underscore ignored
        "status": "success",  # Status word ignored
        "message": "ok",  # Message word ignored
    }
    events = detector.inspect_response(EntityType.EMPLOYEE, mapping=mapping, raw_response=resp)
    assert len(events) == 1
    assert events[0].field_name == "custom_tax_code"
    assert events[0].severity == DriftSeverity.INFO
    assert len(detector.history) == 1

    # Dict mapping fallback
    dict_events = detector.inspect_response(
        EntityType.EMPLOYEE,
        mapping={"id": "id", "name": "name"},
        raw_response={"id": "1", "name": "Test", "extra_col": 123},
    )
    assert len(dict_events) == 1
    assert dict_events[0].field_name == "extra_col"


def test_schema_drift_inspect_error_payload_non_dict():
    detector = SchemaDriftInterceptor()
    event_str = detector.inspect_error_payload(
        EntityType.EMPLOYEE,
        host_error_response="Host 400 Bad Request: missing required field 'department_id'",
    )
    assert event_str.severity == DriftSeverity.CRITICAL
    assert event_str.field_name == "department_id"

    event_none = detector.inspect_error_payload(EntityType.EMPLOYEE, host_error_response=None)
    assert event_none.field_name == "unknown_field"


# ==============================================================================
# SECTION 3: INTROSPECTOR, SYNTHESIZER & PROBER
# ==============================================================================


def test_discovered_entity_and_introspection_result_lookups():
    ent = DiscoveredEntity(name="Worker", fields={"emp_code": DiscoveredField(name="emp_code", path="emp_code")})
    assert ent.get_field("emp_code") is not None
    assert ent.get_field("non_existent") is None

    res = IntrospectionResult(title="Test API", version="1.0", entities={"Worker": ent})
    assert res.get_entity("worker") == ent
    assert res.get_entity("MissingEntity") is None


def test_schema_introspector_file_loaders(tmp_path: Path):
    yaml_file = tmp_path / "schema.yaml"
    yaml_file.write_text(
        """
info:
  title: Mock HRMS API
  version: 2.1.0
definitions:
  Candidate:
    type: object
    required: [id]
    properties:
      id:
        type: string
paths:
  /candidates:
    get:
      summary: List candidates
""",
        encoding="utf-8",
    )
    res_yaml = SchemaIntrospector.introspect_file(yaml_file)
    assert res_yaml.title == "Mock HRMS API"
    assert "Candidate" in res_yaml.entities

    json_file = tmp_path / "schema.json"
    json_file.write_text(
        json.dumps(
            {
                "info": {"title": "JSON HRMS", "version": "1.0"},
                "components": {
                    "schemas": {
                        "Employee": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "birth_date": {"type": "string", "format": "date"},
                                "created_at": {"type": "string", "format": "date-time"},
                                "is_active": {"type": "boolean"},
                                "rating": {"type": "number"},
                                "skills": {"type": "array"},
                                "details": {"type": "object"},
                            },
                        }
                    }
                },
                "paths": {
                    "/api/v1/employees": {"get": {}, "post": {}},
                    "/api/v2/companies": {"get": {}},
                    "/rest/root": {"get": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    res_json = SchemaIntrospector.introspect_file(json_file)
    assert res_json.title == "JSON HRMS"
    emp = res_json.get_entity("Employee")
    assert emp is not None
    assert emp.fields["birth_date"].data_type == FieldDataType.DATE
    assert emp.fields["created_at"].data_type == FieldDataType.DATETIME
    assert emp.fields["is_active"].data_type == FieldDataType.BOOLEAN
    assert emp.fields["rating"].data_type == FieldDataType.NUMBER
    assert emp.fields["skills"].data_type == FieldDataType.ARRAY
    assert emp.fields["details"].data_type == FieldDataType.OBJECT

    with pytest.raises(FileNotFoundError):
        SchemaIntrospector.introspect_file(tmp_path / "does_not_exist.yaml")


def test_schema_introspector_json_samples_inference_and_heuristics():
    empty_ent = SchemaIntrospector.introspect_json_samples("JobOpening", [])
    assert empty_ent.name == "JobOpening"
    assert len(empty_ent.fields) == 0

    samples = [
        {
            "requisition_id": "REQ-01",
            "target_headcount": 5,
            "is_remote": True,
            "budget": 150000.50,
            "tags": ["Urgent", "Growth"],
            "meta": {"prio": 1},
            "open_date": "2026-10-01",
            "published_at": "2026-10-01T08:00:00Z",
            "numeric_code_str": "12345",
            "float_str": "99.95",
            "notes": "Urgent hire for platform team",
        },
        {
            "requisition_id": "REQ-02",
            "target_headcount": 2,
            "is_remote": False,
            "budget": 120000.00,
            "tags": ["Replacement"],
            "meta": {"prio": 2},
            "open_date": "2026-10-15",
            "published_at": "2026-10-15T09:30:00Z",
            "numeric_code_str": "67890",
            "float_str": "88.50",
            "notes": None,
        },
    ]
    discovered = SchemaIntrospector.introspect_json_samples("Requisition", samples)
    assert discovered.id_field == "requisition_id"
    assert discovered.fields["target_headcount"].data_type == FieldDataType.INTEGER
    assert discovered.fields["is_remote"].data_type == FieldDataType.BOOLEAN
    assert discovered.fields["budget"].data_type == FieldDataType.NUMBER
    assert discovered.fields["tags"].data_type == FieldDataType.ARRAY
    assert discovered.fields["meta"].data_type == FieldDataType.OBJECT
    assert discovered.fields["open_date"].data_type == FieldDataType.DATE
    assert discovered.fields["published_at"].data_type == FieldDataType.DATETIME
    assert discovered.fields["numeric_code_str"].data_type == FieldDataType.INTEGER
    assert discovered.fields["float_str"].data_type == FieldDataType.NUMBER
    assert discovered.fields["notes"].is_nullable is True


def test_schema_synthesizer_generic_domain_and_enum_alignment():
    discovered = DiscoveredEntity(
        name="AssetInventory",
        id_field="asset_code",
        fields={
            "asset_code": DiscoveredField(name="asset_code", path="asset_code", data_type=FieldDataType.STRING),
            "asset_cost": DiscoveredField(name="asset_cost", path="asset_cost", data_type=FieldDataType.NUMBER),
            "warranty_years": DiscoveredField(
                name="warranty_years", path="warranty_years", data_type=FieldDataType.INTEGER
            ),
            "purchase_date": DiscoveredField(
                name="purchase_date", path="purchase_date", data_type=FieldDataType.DATE
            ),
            "last_audit_at": DiscoveredField(
                name="last_audit_at", path="last_audit_at", data_type=FieldDataType.DATETIME
            ),
            "is_assigned": DiscoveredField(name="is_assigned", path="is_assigned", data_type=FieldDataType.BOOLEAN),
            "condition": DiscoveredField(
                name="condition",
                path="condition",
                data_type=FieldDataType.STRING,
                enum_values=["BRAND_NEW", "GOOD_CONDITION", "DAMAGED"],
            ),
        },
    )
    mapping, report = MappingSynthesizer.synthesize(discovered, "UNKNOWN_CUSTOM_DOMAIN")
    assert mapping.id_path == "asset_code"
    targets = {fm.target: fm.transform for fm in mapping.fields}
    assert targets["asset_cost"] == "decimal"
    assert targets["warranty_years"] == "int"
    assert targets["purchase_date"] == "date"
    assert targets["last_audit_at"] == "datetime"
    assert targets["is_assigned"] == "bool"

    # Test _align_enums substring match
    aligned = MappingSynthesizer._align_enums(["GOOD_CONDITION"], {"GOOD": "ACCEPTABLE"})
    assert aligned.get("GOOD_CONDITION") == "ACCEPTABLE"


@pytest.mark.asyncio
async def test_schema_synthesizer_agentic_with_markdown_fence():
    discovered = DiscoveredEntity(
        name="JobRequisition",
        id_field="id",
        fields={
            "id": DiscoveredField(name="id", path="id", data_type=FieldDataType.STRING),
            "position_name": DiscoveredField(
                name="position_name", path="position_name", data_type=FieldDataType.STRING
            ),
        },
    )

    class MockLlmGateway:
        async def chat(self, *args, **kwargs):
            from hrms_plugin.ai.llm import LLMResponse

            # Wrapped in markdown code block with leading backticks
            llm_json = (
                '```json\n{"title": {"host_field": "position_name", "confidence": 0.95, '
                '"rationale": "Direct synonym"}}\n```'
            )
            return LLMResponse(content=llm_json)

        async def aclose(self):
            pass

    mapping, report = await MappingSynthesizer.synthesize_agentic(
        discovered,
        EntityType.REQUISITION,
        llm_gateway=MockLlmGateway(),
    )
    targets = [fm.target for fm in mapping.fields]
    assert "title" in targets


def test_shadow_prober_validation_warnings_and_exceptions():
    assert ShadowProber.probe(EntityMapping(vendor="v", entity_type="e", id_path="id"), []).is_valid is False

    mapping = EntityMapping(
        vendor="iceipts",
        entity_type="REQUISITION",
        id_path="id",
        fields=(
            FieldMap(target="headcount", source="count", transform="decimal", default="INVALID_NUM"),
            FieldMap(target="open_date", source="dt", transform="date", default="INVALID_DATE"),
        ),
    )
    records = [
        {"id": "", "count": None, "dt": None},  # empty PK + default string triggering type warning
        {"id": "REQ-1", "count": 3, "dt": "2026-10-29"},
    ]
    report = ShadowProber.probe(mapping, records, required_canonical_fields=["headcount"])
    assert report.is_valid is False
    assert report.warning_count >= 1
    assert any(i.severity == IssueSeverity.ERROR and i.message == "MappingError" for i in report.issues)
    assert any(i.severity == IssueSeverity.WARNING and "expected Decimal" in i.message for i in report.issues)


# ==============================================================================
# SECTION 4: STATUTORY PAYROLL AGENT
# ==============================================================================


@pytest.mark.parametrize(
    "raw_in,expected_out",
    [
        (100, Decimal("100")),
        (12.345, Decimal("12.35")),
        ("1500.50", Decimal("1500.50")),
        (Decimal("42.00"), Decimal("42.00")),
    ],
)
def test_statutory_payroll_decimal_helper(raw_in: Any, expected_out: Decimal):
    assert _d(raw_in) == expected_out


def test_statutory_payroll_india_salary_structure():
    agent = StatutoryPayrollAgent()

    # Standard high CTC (> 21,000 INR monthly gross) -> No ESI
    res_high = agent.structure_india_salary(annual_ctc=1800000.0)
    assert res_high.annual_ctc == 1800000.0
    assert res_high.basic_salary == 60000.0
    assert res_high.employee_pf == 7200.0
    assert res_high.employee_esi == 0.0
    assert res_high.employer_esi == 0.0
    assert res_high.professional_tax == 200.0
    assert len(res_high.statutory_citations) >= 1

    # Low CTC (<= 21,000 INR monthly gross) -> Triggers ESI (0.75% emp, 3.25% empr)
    res_low = agent.structure_india_salary(annual_ctc=200000.0)
    assert res_low.gross_salary <= 21000.0
    assert res_low.employee_esi > 0.0
    assert res_low.employer_esi > 0.0

    # Capped PF at 15,000 ceiling
    res_capped = agent.structure_india_salary(annual_ctc=1800000.0, cap_pf_at_ceiling=True)
    assert res_capped.employee_pf == 1800.0  # 12% of 15,000

    # Negative balancing component check
    res_tight = agent.structure_india_salary(annual_ctc=60000.0, basic_percent=70.0, hra_percent=40.0)
    assert res_tight.special_allowance == 0.0


def test_statutory_payroll_uae_salary_structure():
    agent = StatutoryPayrollAgent()
    res = agent.structure_uae_salary(monthly_gross=20000.0)
    assert res.monthly_gross == 20000.0
    assert res.basic_wage == 12000.0  # 60%
    assert res.housing_allowance == 6000.0  # 30%
    assert res.transport_allowance == 2000.0  # 10%
    assert res.other_allowances == 0.0
    assert res.wps_compliant is True
    assert res.estimated_monthly_eosb_accrual > 0.0
    assert len(res.statutory_citations) >= 1


@pytest.mark.parametrize(
    "tenure,basic,expected_eosb,capped",
    [
        (0.5, 10000.0, 0.0, False),  # < 1 yr: 0
        (1.0, 10000.0, 7000.0, False),  # 1 yr: 21 days = 10000*21/30 = 7000
        (3.0, 10000.0, 21000.0, False),  # 3 yrs: 21000
        (5.0, 10000.0, 35000.0, False),  # 5 yrs: 35000
        (8.0, 10000.0, 65000.0, False),  # 8 yrs: 5*7000 + 3*10000 = 65000
        (30.0, 10000.0, 240000.0, True),  # 30 yrs: Capped at 24 * monthly basic
    ],
)
def test_statutory_payroll_uae_eosb_bounds(tenure: float, basic: float, expected_eosb: float, capped: bool):
    agent = StatutoryPayrollAgent()
    calc = agent.calculate_uae_eosb(basic_wage_monthly=basic, tenure_years=tenure)
    assert calc.total_eosb_gratuity == expected_eosb
    if capped:
        assert "Capped" in calc.formula_breakdown
    assert "Article 51" in calc.statutory_citation


@pytest.mark.parametrize(
    "tenure,basic,expected_gratuity,capped",
    [
        (4.5, 50000.0, 0.0, False),  # < 5 yrs: 0
        (5.0, 50000.0, 144230.77, False),  # 15 * 50000 * 5 / 26
        (7.0, 50000.0, 201923.08, False),  # 15 * 50000 * 7 / 26
        (40.0, 100000.0, 2000000.0, True),  # Exceeds 20 Lakhs statutory cap
    ],
)
def test_statutory_payroll_india_gratuity_bounds(
    tenure: float, basic: float, expected_gratuity: float, capped: bool
):
    agent = StatutoryPayrollAgent()
    calc = agent.calculate_india_gratuity(last_drawn_basic=basic, tenure_years=tenure)
    assert calc.gratuity_amount == expected_gratuity
    if capped:
        assert "2,000,000" in calc.formula_breakdown


# ==============================================================================
# SECTION 5: RECRUITMENT, LEAVE & ONBOARDING AGENTS
# ==============================================================================


def test_recruitment_agent_scoring_and_composite_bands():
    agent = RecruitmentAgent()
    # 0 experience required
    zero_exp = agent.score_candidate(
        resume_text="Senior Python React Engineer with Docker and AWS experience",
        required_skills=["Python", "React", "Docker"],
        required_experience_years=0.0,
        candidate_experience_years=5.0,
    )
    assert zero_exp.experience_score == 100.0

    # Shortlist recommendation (>= 75.0)
    assert zero_exp.recommendation == "SHORTLIST"

    # Review Band recommendation (55.0 to 74.9)
    review = agent.score_candidate(
        resume_text="Developer with Python and React knowledge and a bachelor degree",
        required_skills=["Python", "React", "Docker"],
        required_experience_years=4.0,
        candidate_experience_years=3.0,
    )
    assert review.recommendation == "REVIEW_BAND"

    # Reject recommendation (< 55.0)
    reject = agent.score_candidate(
        resume_text="Office clerk with word processing skills",
        required_skills=["Python", "FastAPI", "PostgreSQL", "Docker", "Terraform"],
        required_experience_years=6.0,
        candidate_experience_years=0.0,
    )
    assert reject.recommendation == "REJECT"


def test_recruitment_agent_deduplication_without_emails():
    agent = RecruitmentAgent()
    # Deduplicate strictly across phone numbers and LinkedIn handles without emails
    candidates = [
        {"id": "C-1", "full_name": "Tariq Mansoor", "phone": "+971 50 111 2233", "channel": "PORTAL"},
        {
            "id": "C-2",
            "full_name": "Tariq M",
            "phone": "+971501112233",
            "linkedin": "https://linkedin.com/in/tmansoor",
            "channel": "AGENCY",
        },
        {"id": "C-3", "full_name": "Unique Worker", "phone": "+971509998877"},
    ]
    merged = agent.deduplicate_candidates(candidates)
    assert len(merged) == 2
    primary = next(c for c in merged if "tmansoor" in (c.linkedin_url or ""))
    assert primary.application_count == 2
    assert "PORTAL" in primary.merged_source_channels
    assert "AGENCY" in primary.merged_source_channels
    assert primary.phone == "971501112233"


def test_leave_attendance_agent_evaluations_and_boundaries():
    agent = LeaveAttendanceAgent()

    # Boundary: requested_days <= 0
    res_zero = agent.evaluate_leave_request("EMP-1", "ANNUAL", 0.0, 10.0)
    assert res_zero.is_approved is False
    assert "greater than 0" in res_zero.rejection_reason

    # Boundary: Probation with insufficient balance
    res_prob = agent.evaluate_leave_request(
        "EMP-1", "ANNUAL", 5.0, 2.0, is_on_probation=True, allow_negative_balance=False
    )
    assert res_prob.is_approved is False
    assert res_prob.requires_manager_override is True

    # Negative balance allowed
    res_neg = agent.evaluate_leave_request("EMP-1", "ANNUAL", 5.0, 2.0, allow_negative_balance=True)
    assert res_neg.is_approved is True
    assert res_neg.remaining_balance == -3.0
    assert res_neg.requires_manager_override is True


def test_leave_attendance_agent_impossible_travel():
    agent = LeaveAttendanceAgent()

    # Unparseable timestamp negative path
    bad_time = agent.detect_impossible_travel(
        {"lat": 25.2, "lng": 55.27, "timestamp": "not-iso"},
        {"lat": 28.6, "lng": 77.2, "timestamp": "also-not-iso"},
    )
    assert bad_time.is_anomaly is False
    assert "Could not parse" in bad_time.explanation

    # Delta hours == 0 (same instant) with same coords -> speed 0
    same_instant = agent.detect_impossible_travel(
        {"lat": 25.2000, "lng": 55.2700, "timestamp": "2026-10-29T08:00:00Z"},
        {"lat": 25.2001, "lng": 55.2701, "timestamp": "2026-10-29T08:00:00Z"},
    )
    assert same_instant.is_anomaly is False
    assert same_instant.speed_kmh == 0.0

    # Delta hours == 0 with distant coords -> speed 999999
    teleport = agent.detect_impossible_travel(
        {"lat": 25.2000, "lng": 55.2700, "timestamp": "2026-10-29T08:00:00Z"},
        {"lat": 28.6000, "lng": 77.2000, "timestamp": "2026-10-29T08:00:00Z"},
    )
    assert teleport.is_anomaly is True
    assert teleport.speed_kmh == 999999.0


def test_onboarding_agent_offer_evaluation_bands():
    agent = OnboardingAgent()

    # Band ceiling exceeded
    res_high = agent.evaluate_offer(
        candidate_name="Kareem",
        job_title="Lead Architect",
        annual_salary=180000.0,
        currency="AED",
        band_max=150000.0,
    )
    assert res_high.status == OfferStatus.BAND_VIOLATION
    assert res_high.requires_executive_approval is True

    # Band floor violated
    res_low = agent.evaluate_offer(
        candidate_name="Kareem",
        job_title="Lead Architect",
        annual_salary=80000.0,
        currency="AED",
        band_min=100000.0,
    )
    assert res_low.status == OfferStatus.BAND_VIOLATION

    # UAE minimum wage check (< 60,000 AED annually)
    res_min_wage = agent.evaluate_offer(
        candidate_name="Kareem",
        job_title="Assistant",
        annual_salary=48000.0,
        currency="AED",
        jurisdiction="AE",
    )
    assert res_min_wage.status == OfferStatus.BELOW_MINIMUM_WAGE
    assert "Article 27" in res_min_wage.statutory_citation


def test_onboarding_agent_kyc_document_verifications():
    agent = OnboardingAgent()

    # India: missing PAN and invalid Aadhaar
    in_fail = agent.verify_onboarding_documents(
        candidate_name="Ravi Kumar",
        jurisdiction=Jurisdiction.INDIA,
        submitted_documents={"aadhaar": "1234"},  # Not 12 digits
    )
    assert in_fail.status == DocumentVerificationStatus.INVALID_FORMAT
    assert "PAN Card" in in_fail.missing_documents
    assert any(i["document"] == "Aadhaar Card" for i in in_fail.invalid_documents)

    # India: valid
    in_pass = agent.verify_onboarding_documents(
        candidate_name="Ravi Kumar",
        jurisdiction=Jurisdiction.INDIA,
        submitted_documents={
            "pan": "ABCDE1234F",
            "aadhaar": "2345 6789 0125",
            "bank_account": "987654321012",
            "ifsc": "HDFC0001234",
        },
    )
    assert in_pass.status == DocumentVerificationStatus.VERIFIED

    # UAE: invalid Emirates ID and missing ILOE
    uae_fail = agent.verify_onboarding_documents(
        candidate_name="Hamad Al-Falasi",
        jurisdiction=Jurisdiction.UAE,
        submitted_documents={
            "emirates_id": "123-456",  # Invalid pattern
            "passport": "P1234567",
        },
    )
    assert uae_fail.status == DocumentVerificationStatus.INVALID_FORMAT
    assert any(i["document"] == "Emirates ID" for i in uae_fail.invalid_documents)
    assert "ILOE Unemployment Insurance Certificate" in uae_fail.missing_documents

    # UAE: valid
    uae_pass = agent.verify_onboarding_documents(
        candidate_name="Hamad Al-Falasi",
        jurisdiction=Jurisdiction.UAE,
        submitted_documents={
            "emirates_id": "784-1990-1234567-1",
            "passport": "P1234567",
            "iloe_certificate": "ILOE-CERT-99",
        },
    )
    assert uae_pass.status == DocumentVerificationStatus.VERIFIED


def test_onboarding_agent_provisioning_plans():
    agent = OnboardingAgent()
    dev_plan = agent.generate_provisioning_plan("EMP-10", "Dev User", "Tech", "Senior Software Developer")
    assert any("YubiKey" in a for a in dev_plan.it_assets)

    hr_plan = agent.generate_provisioning_plan("EMP-11", "HR User", "People", "HR Generalist")
    assert not any("YubiKey" in a for a in hr_plan.it_assets)


# ==============================================================================
# SECTION 6: TOOL REGISTRY HANDLERS
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_registry_handler_error_and_return_types():
    registry = AgenticToolRegistry()

    # Unregistered tool
    err_res = await registry.execute("unknown_tool", {})
    assert "error" in err_res and "not registered" in err_res["error"]

    # Tool returning model_dump
    class MockModel:
        def model_dump(self):
            return {"key": "dumped_value"}

    registry.register(
        ToolDefinition(name="mock_model_tool", description="returns model", parameters={"type": "object"}),
        lambda: MockModel(),
        intent=UserIntent.GENERAL_HR,
        permission=Permission.VIEW_EMPLOYEE,
    )
    res_model = await registry.execute("mock_model_tool", {})
    assert res_model == {"key": "dumped_value"}

    # Tool returning non-dict non-primitive object
    class CustomScalar:
        def __str__(self):
            return "CUSTOM_STRING_REPR"

    registry.register(
        ToolDefinition(name="custom_scalar_tool", description="returns custom scalar", parameters={"type": "object"}),
        lambda: CustomScalar(),
        intent=UserIntent.GENERAL_HR,
        permission=Permission.VIEW_EMPLOYEE,
    )
    res_scalar = await registry.execute("custom_scalar_tool", {})
    assert res_scalar == {"result": "CUSTOM_STRING_REPR"}

    # Tool throwing exception
    registry.register(
        ToolDefinition(name="exploding_tool", description="raises error", parameters={"type": "object"}),
        lambda: 1 / 0,
        intent=UserIntent.GENERAL_HR,
        permission=Permission.VIEW_EMPLOYEE,
    )
    res_ex = await registry.execute("exploding_tool", {})
    assert "division by zero" in res_ex["error"]


@pytest.mark.asyncio
async def test_tool_registry_default_tools_execution():
    registry = AgenticToolRegistry()

    # calculate_salary_structure (UAE vs India)
    uae_sal = await registry.execute(
        "calculate_salary_structure",
        {"gross_or_ctc": 25000.0, "jurisdiction": "AE"},
    )
    assert uae_sal["monthly_gross"] == 25000.0

    in_sal = await registry.execute(
        "calculate_salary_structure",
        {"gross_or_ctc": 1200000.0, "jurisdiction": "IN"},
    )
    assert in_sal["annual_ctc"] == 1200000.0

    # calculate_eosb_gratuity (UAE vs India)
    uae_eosb = await registry.execute(
        "calculate_eosb_gratuity",
        {"basic_wage": 10000.0, "tenure_years": 3.0, "jurisdiction": "AE"},
    )
    assert uae_eosb["total_eosb_gratuity"] == 21000.0

    in_grat = await registry.execute(
        "calculate_eosb_gratuity",
        {"basic_wage": 50000.0, "tenure_years": 7.0, "jurisdiction": "IN"},
    )
    assert in_grat["gratuity_amount"] > 0.0

    # Lazy payroll service access
    assert registry.payroll_service is not None

    # detect_travel_anomaly
    travel_res = await registry.execute(
        "detect_travel_anomaly",
        {
            "punch1": {"lat": 25.2, "lng": 55.27, "timestamp": "2026-10-29T08:00:00Z"},
            "punch2": {"lat": 28.6, "lng": 77.2, "timestamp": "2026-10-29T09:00:00Z"},
        },
    )
    assert travel_res["is_anomaly"] is True

    # score_candidate_ats
    ats_res = await registry.execute(
        "score_candidate_ats",
        {"resume_text": "Experienced Python Engineer", "required_skills": ["Python"],
         "required_experience_years": 3.0, "candidate_experience_years": 3.0},
    )
    assert ats_res["composite_score"] > 0.0

    # audit_job_description_bias
    bias_res = await registry.execute("audit_job_description_bias", {"jd_text": "Seeking a ninja developer"})
    assert bias_res["has_bias"] is True

    # verify_onboarding_documents
    doc_res = await registry.execute(
        "verify_onboarding_documents",
        {
            "candidate_name": "Hamad",
            "jurisdiction": "AE",
            "submitted_documents": {"passport": "P12345"},
        },
    )
    assert doc_res["status"] in ("INVALID_FORMAT", "MISSING_DOCUMENTS")

    # audit_offboarding_fnf_timeline
    fnf_res = await registry.execute(
        "audit_offboarding_fnf_timeline",
        {
            "employee_id": "EMP-10",
            "employee_name": "Rashid",
            "last_working_day": "2026-10-15",
            "current_date": "2026-10-29",
        },
    )
    assert "days_elapsed" in fnf_res

    # compute_final_settlement
    settle_res = await registry.execute(
        "compute_final_settlement",
        {
            "employee_id": "EMP-10",
            "employee_name": "Rashid",
            "basic_wage_monthly": 10000.0,
            "gross_wage_monthly": 16000.0,
            "tenure_years": 3.0,
            "jurisdiction": "AE",
        },
    )
    assert settle_res["total_net_payable"] > 0.0

    # audit_travel_expense_claim
    exp_res = await registry.execute(
        "audit_travel_expense_claim",
        {
            "claim_id": "CLM-01",
            "employee_id": "EMP-10",
            "expenses": [{"category": "FLIGHT", "amount": 1200.0}],
            "currency": "AED",
        },
    )
    assert exp_res["claim_id"] == "CLM-01"


# ==============================================================================
# SECTION 7: SUPERVISOR & ORCHESTRATOR REACT FLOW
# ==============================================================================


def test_supervisor_agent_routing_and_uncovered_paths():
    supervisor = SupervisorAgent()

    # PAYROLL_STRUCTURE with UAE
    uae_res = supervisor.process_message(
        message="Calculate salary structure for UAE employee",
        jurisdiction=Jurisdiction.UAE,
        context={"ctc": 240000.0},
        auth=HR_MGR_AUTH,
    )
    assert uae_res.intent == UserIntent.PAYROLL_STRUCTURE
    assert uae_res.routed_agent == "StatutoryPayrollAgent"
    assert "AED" in uae_res.reply_text

    # ONBOARDING with provision
    prov_res = supervisor.process_message(
        message="Generate IT provisioning plan for new hire",
        jurisdiction=Jurisdiction.UAE,
        context={"candidate_name": "Rashid", "employee_id": "EMP-500", "role": "Site Reliability Engineer"},
        auth=HR_MGR_AUTH,
    )
    assert prov_res.intent == UserIntent.ONBOARDING
    assert prov_res.routed_agent == "OnboardingAgent"
    assert "Provisioning plan generated" in prov_res.reply_text

    # OFFBOARDING_FNF with clearance
    clr_res = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        context={"employee_id": "EMP-500", "employee_name": "Rashid", "finance_cleared": True},
        auth=HR_MGR_AUTH,
    )
    assert clr_res.intent == UserIntent.OFFBOARDING_FNF
    assert clr_res.routed_agent == "OffboardingAgent"
    assert "Exit clearance" in clr_res.reply_text


def test_orchestrator_collect_from_output_branches():
    state = _RunState()
    output = {
        "statutory_citation": "Citation String",
        "statutory_citations": ["Citation A", "Citation B"],
        "citations": [{"citation": "Dict Citation"}],
        "violations": [
            {
                "violation_id": "V-101",
                "entity_id": "EMP-101",
                "entity_type": "employee",
                "statutory_citation": "Violation Citation",
                "remediation_patch": {"salary": 60000.0},
            }
        ],
        "suggested_actions": [{"action": "ACTION_SUGGESTED"}],
        "actions": [{"action": "ACTION_DIRECT"}],
    }
    _collect_from_output(output, state)
    assert "Citation String" in state.citations
    assert "Citation A" in state.citations
    assert "Dict Citation" in state.citations
    assert "Violation Citation" in state.citations
    assert any(a.get("action") == "REMEDIATE_PATCH" for a in state.actions)
    assert any(a.get("action") == "ACTION_SUGGESTED" for a in state.actions)
    assert any(a.get("action") == "ACTION_DIRECT" for a in state.actions)


@pytest.mark.asyncio
async def test_orchestrator_run_stream_with_tool_calls(scripted):
    gateway, _ = scripted(
        tool_call("calculate_salary_structure", gross_or_ctc=1200000.0, jurisdiction="IN"),
        final("Final compliant salary structure synthesized."),
    )
    orchestrator = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    events = []
    async for chunk in orchestrator.run_stream(
        user_message="Calculate India CTC structure for 1200000",
        jurisdiction="IN",
        auth=HR_MGR_AUTH,
    ):
        events.append(chunk)

    event_names = [e.get("event") for e in events]
    assert "thinking" in event_names
    assert "tool_call" in event_names
    assert "tool_observation" in event_names
    assert "token" in event_names
    assert "intent" in event_names


# ==============================================================================
# SECTION 8: MCP SERVER TOOLS
# ==============================================================================


def test_mcp_server_all_tools_execution():
    """P11: over MCP, number-taking calculators are refused and lookups run through the validated registry."""
    import asyncio

    from hrms_plugin.security.rbac import AuthContext, UserRole

    mcp = HrmsMcpServer(AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN})),
                        jurisdiction="AE")
    names = {t.name for t in mcp.list_tools()}
    assert "hrms_search_labor_regulations" in names and "hrms_audit_job_description_bias" in names
    for refused in ("hrms_calculate_eosb_gratuity", "hrms_score_candidate_ats", "hrms_detect_travel_anomaly",
                    "hrms_evaluate_leave_request"):
        assert refused not in names
        assert asyncio.run(mcp.call_tool(refused, {}))["error"] == "TOOL_NOT_AVAILABLE"
    bias = asyncio.run(mcp.call_tool("hrms_audit_job_description_bias", {"jd_text": "Looking for rockstar ninja"}))
    assert bias["has_bias"] is True


# ==============================================================================
# SECTION 9: PII PROTECTION & DEFECT PINNING
# ==============================================================================


def test_pii_iban_scanner_break_conditions():
    # Tests line 127 in _iban_matches: break on non-alnum / non-matching space
    assert _iban_matches("iban GB82.WEST.1234.5698.7654.32") == []
    assert _iban_matches("iban GB82  WEST  1234") == []


@pytest.mark.parametrize(
    "val,expected_grouping",
    [
        (500, "500"),
        (45000, "45,000"),
        (1800000, "18,00,000"),
        (10000000, "1,00,00,000"),
    ],
)
def test_pii_indian_grouping(val: int, expected_grouping: str):
    assert _indian_grouping(val) == expected_grouping


def test_pii_money_variants():
    assert money_variants("invalid-number") == []
    assert money_variants(float("inf")) == []
    assert money_variants(float("nan")) == []

    variants_whole = money_variants(50000)
    assert "50000" in variants_whole
    assert "50,000" in variants_whole
    assert "50000.00" in variants_whole

    variants_frac = money_variants(50000.75)
    assert any("50,000.75" in v for v in variants_frac)


def test_pii_session_redact_restore_and_inspection():
    s = PiiSession()
    assert s.redact_text("") == ""
    assert s.restore("") == ""
    assert s.find_pii("") == []
    assert s.restore_obj({"key": "val", "list": [1, 2]}) == {"key": "val", "list": [1, 2]}

    s.learn({"EmployeeName": "Tariq Mansoor", "Phone": "+971501234567"})
    assert s.minted_counts() == {}
    redacted = s.redact_text("Meeting with Tariq Mansoor at +971501234567")
    assert "Tariq" not in redacted
    assert "+971501234567" not in redacted
    assert s.minted_counts()["NAME"] == 1
    assert s.minted_counts()["PHONE"] == 1
    restored = s.restore(redacted)
    assert restored == "Meeting with Tariq Mansoor at +971501234567"


# PRODUCT DEFECT PIN: PiiSession._learn_money omits Decimal in isinstance check
# ==============================================================================
# SECTION 10: HTTP-LEVEL INTEGRATION VIA FASTAPI TESTCLIENT
# ==============================================================================


def test_http_chat_payroll_routing_uae_non_agentic(make_client, bearer):
    client = make_client()
    res = client.post(
        "/v1/chat",
        json={
            "message": "Calculate salary structure for UAE monthly gross 20000 AED",
            "jurisdiction": "AE",
            "context": {"ctc": 240000.0},
            "agentic": False,
        },
        headers=bearer(roles=["hr_manager"]),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "StatutoryPayrollAgent"
    assert data["structured_data"]["basic_wage"] == 12000.0


def test_http_chat_onboarding_provisioning_non_agentic(make_client, bearer):
    client = make_client()
    res = client.post(
        "/v1/chat",
        json={
            "message": "Generate provision plan for new hire",
            "jurisdiction": "AE",
            "context": {
                "candidate_name": "Tariq Mansoor",
                "employee_id": "EMP-900",
                "role": "Cloud DevOps Engineer",
            },
            "agentic": False,
        },
        headers=bearer(roles=["hr_manager"]),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "ONBOARDING"
    assert data["routed_agent"] == "OnboardingAgent"
    assert any("YubiKey" in item for item in data["structured_data"]["it_assets"])


def test_http_chat_offboarding_clearance_non_agentic(make_client, bearer):
    client = make_client()
    res = client.post(
        "/v1/chat",
        json={
            "message": "Evaluate exit clearance status",
            "jurisdiction": "AE",
            "context": {
                "employee_name": "Tariq Mansoor",
                "employee_id": "EMP-900",
                "returned_assets": ["Laptop"],
                "pending_assets": [],
                "finance_cleared": True,
            },
            "agentic": False,
        },
        headers=bearer(roles=["hr_manager"]),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "OFFBOARDING_FNF"
    assert data["routed_agent"] == "OffboardingAgent"
    assert data["structured_data"]["is_fully_cleared"] is True


def test_http_chat_negative_auth_and_permissions(make_client, bearer):
    client = make_client()

    # Missing authorization header
    res_no_auth = client.post("/v1/chat", json={"message": "hello"})
    assert res_no_auth.status_code == 401

    # Malformed bearer token
    res_bad_tok = client.post("/v1/chat", json={"message": "hello"}, headers={"Authorization": "Bearer bad.token.here"})
    assert res_bad_tok.status_code == 401

    # Employee role lacks VIEW_SALARY permission for payroll structuring
    res_forbidden = client.post(
        "/v1/chat",
        json={
            "message": "Calculate salary structure for 1800000",
            "jurisdiction": "IN",
            "agentic": False,
        },
        headers=bearer(roles=["employee"]),
    )
    assert res_forbidden.status_code == 403


def test_http_chat_stream_agentic_react_loop(make_client, bearer, scripted):
    gateway, _ = scripted(
        tool_call("calculate_salary_structure", gross_or_ctc=1200000.0, jurisdiction="IN"),
        final("Compliant structure prepared."),
    )
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    res = client.post(
        "/v1/chat/stream",
        json={
            "message": "Calculate salary structure for 1200000 INR",
            "jurisdiction": "IN",
        },
        headers=bearer(roles=["hr_manager"]),
    )
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    body = res.text
    assert "tool_call" in body
    assert "tool_observation" in body
    assert "calculate_salary_structure" in body
    assert "PAYROLL_STRUCTURE" in body


# ==============================================================================
# SECTION 11: CROSS-COMPONENT ADVANCED EDGE CASES & GAP CLOSURES
# ==============================================================================


def test_mapping_join_names_tuple_source():
    fm = FieldMap(target="full_name", source=("first_name", "last_name"), transform="join_names")
    assert fm.apply({"first_name": "Tariq", "last_name": "Mansoor"}) == "Tariq Mansoor"


def test_mapping_decimal_invalid_operation():
    assert TRANSFORMS["decimal"]("not_a_valid_decimal") is None
    assert TRANSFORMS["decimal"](None) is None


def test_resolve_path_nested_scalar_and_none():
    assert resolve_path({"missing": None}, "missing[0]") is None
    assert resolve_path("string_scalar", "field") is None
    assert TRANSFORMS["list"]([None, "scalar"]) == ["scalar"]


def test_introspector_ref_resolution_and_tags():
    spec = {
        "info": {"title": "Ref API", "version": "1.0"},
        "components": {
            "schemas": {
                "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
                "Person": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "addr": {"$ref": "#/components/schemas/Address"},
                    },
                },
            }
        },
        "paths": {"/persons": {"get": {"tags": ["Person"]}}},
    }
    result = SchemaIntrospector.introspect_openapi(spec)
    assert "Person" in result.entities
    assert result.entities["Person"].read_endpoint == "GET /persons"
    assert SchemaIntrospector._guess_entity_name_from_path("/") == "Root"


def test_introspector_suffix_id_inference():
    samples = [{"custom_key_id": "1001", "name": "Test"}]
    discovered = SchemaIntrospector.introspect_json_samples("CustomEntity", samples)
    assert discovered.id_field == "custom_key_id"


def test_synthesizer_candidate_id_resolution():
    discovered = DiscoveredEntity(
        name="Worker",
        id_field=None,
        fields={"code": DiscoveredField(name="code", path="code", data_type=FieldDataType.STRING)},
    )
    mapping, _ = MappingSynthesizer.synthesize(discovered, "UNKNOWN_DOMAIN")
    assert mapping.id_path == "code"


def test_synthesizer_normalized_token_match_with_enums():
    discovered = DiscoveredEntity(
        name="JobRequisition",
        id_field="id",
        fields={
            "id": DiscoveredField(name="id", path="id", data_type=FieldDataType.STRING),
            "job_title": DiscoveredField(name="job_title", path="job_title", data_type=FieldDataType.STRING),
        },
    )
    mapping, report = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION)
    assert any(fm.target == "title" for fm in mapping.fields)


@pytest.mark.asyncio
async def test_synthesizer_agentic_bare_code_block():
    discovered = DiscoveredEntity(
        name="JobRequisition",
        id_field="id",
        fields={
            "id": DiscoveredField(name="id", path="id", data_type=FieldDataType.STRING),
            "unmapped_role": DiscoveredField(
                name="unmapped_role", path="unmapped_role", data_type=FieldDataType.STRING
            ),
        },
    )

    class MockLlmGatewayBare:
        async def chat(self, *args, **kwargs):
            from hrms_plugin.ai.llm import LLMResponse

            # Triple backticks without "json" keyword
            return LLMResponse(
                content='```\n{"title": {"host_field": "unmapped_role", "confidence": 0.88, '
                '"rationale": "Role matches Title"}}\n```'
            )

        async def aclose(self):
            pass

    mapping, report = await MappingSynthesizer.synthesize_agentic(
        discovered,
        EntityType.REQUISITION,
        llm_gateway=MockLlmGatewayBare(),
    )
    assert any(fm.target == "title" for fm in mapping.fields)


def test_pii_nested_dict_and_list_money_learning():
    s = PiiSession(tokenize_compensation=True)
    s.learn({"salary": {"basic": 50000, "allowances": [10000, {"special": 5000}]}})
    redacted = s.redact_text("Basic 50000, allowance 10000, special 5000")
    assert "50000" not in redacted
    assert "10000" not in redacted
    assert "5000" not in redacted


@pytest.mark.asyncio
async def test_tool_registry_additional_default_tools():
    registry = AgenticToolRegistry()

    # calculate_pf_contribution
    pf_res = await registry.execute(
        "calculate_pf_contribution",
        {"employee_id": "EMP-01", "period": "2026-10", "jurisdiction": "IN", "tenant_id": "acme"},
    )
    assert "employee_id" in pf_res or pf_res.get("error") == "HOST_NOT_CONFIGURED"

    # audit_statutory_compliance
    comp_res = await registry.execute(
        "audit_statutory_compliance",
        {"jurisdiction": "AE", "employees": [{"name": "User 1"}]},
    )
    assert "compliance_score" in comp_res or "violations_count" in comp_res

    # search_labor_regulations
    reg_res = await registry.execute("search_labor_regulations", {"query": "gratuity", "jurisdiction": "IN"})
    assert "citations" in reg_res or "results" in reg_res or isinstance(reg_res, list)

    # evaluate_leave_request
    leave_res = await registry.execute(
        "evaluate_leave_request",
        {"employee_id": "EMP-01", "leave_type": "ANNUAL", "requested_days": 2.0, "current_balance": 10.0},
    )
    assert leave_res["is_approved"] is True

    # evaluate_onboarding_offer
    offer_res = await registry.execute(
        "evaluate_onboarding_offer",
        {
            "candidate_name": "Tariq",
            "job_title": "Engineer",
            "annual_salary": 120000.0,
            "currency": "AED",
            "jurisdiction": "AE",
        },
    )
    assert "status" in offer_res


def test_http_remediate_execute_positive_and_negative(make_client, bearer):
    client = make_client()

    # Positive attribution execution
    payload = {
        "violation_id": "VIO-01",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-01",
        "patch_payload": {"salary": 50000},
    }
    res = client.post(
        "/v1/remediate/execute",
        json=payload,
        headers=bearer(sub="MGR-01", roles=["hr_manager"]),
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ATTRIBUTION_ONLY"

    # Negative: unauthorized without token
    assert client.post("/v1/remediate/execute", json=payload).status_code == 401

    # Negative: forbidden role
    assert (
        client.post(
            "/v1/remediate/execute",
            json=payload,
            headers=bearer(sub="EMP-01", roles=["employee"]),
        ).status_code
        == 403
    )


def test_http_hosts_requisitions_negative_auth(make_client, bearer):
    client = make_client()
    # Unauthenticated
    assert client.get("/v1/hosts/requisitions").status_code == 401
    # Forbidden role
    assert client.get("/v1/hosts/requisitions", headers=bearer(roles=["employee"])).status_code == 403

