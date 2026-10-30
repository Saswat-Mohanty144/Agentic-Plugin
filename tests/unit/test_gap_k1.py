"""Tests for Task K1: PII classification rules and disclosure (decisions K1.1-K1.5).

Covers:
- K1.1: Rules YAML loader and evaluator (validation errors, hit policy, and conditions).
- K1.2: Shipped rule content for the canonical model, four data classes, and fallbacks.
- K1.3: Synthesize suggested_classification and VALUE_CHECK sample cross-check.
- K1.4: Review recomputing rule suggestions while preserving unchanged VALUE_CHECK.
- K1.5: Approve accept_suggested_classification flag, explicit precedence, and classification_origin.
- API-level TestClient flow: synthesize -> review -> approve with accept_suggested_classification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hrms_plugin.governance.classification_rules import (
    ClassificationRules,
    ClassificationRulesError,
    apply_value_check,
    load_rules,
    suggest_classification,
)
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import DATA_CLASSES, MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore

# Fake test data from existing tests (never real secrets)
SAMPLE_PAN = "ABCPE1234F"
SAMPLE_EMAIL = "ravi@acme.test"
ORDINARY_TEXT = "Engineering"


# --------------------------------------------------------------------------------------------------
# K1.1: Rule Loader Errors & Structure Validation
# --------------------------------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "rules.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_loader_unknown_when_key(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      unknown_when_field: true
    then:
      data_class: PII
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="unknown.*when"):
        load_rules(p)


def test_loader_unknown_then_key(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      target_in: [name]
    then:
      data_class: PII
      llm_visible: false
      extra_then_field: 123
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="unknown.*then"):
        load_rules(p)


def test_loader_empty_when(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when: {}
    then:
      data_class: PII
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="empty when"):
        load_rules(p)


def test_loader_duplicate_id(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: dup_id
    when:
      target_in: [name]
    then:
      data_class: PII
      llm_visible: false
    why: test 1
  - id: dup_id
    when:
      target_in: [email]
    then:
      data_class: PII
      llm_visible: false
    why: test 2
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="duplicate.*id"):
        load_rules(p)


def test_loader_unknown_class(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      target_in: [secret_field]
    then:
      data_class: TOP_SECRET
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="unknown.*class"):
        load_rules(p)


def test_loader_llm_visible_true_with_pii(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      target_in: [name]
    then:
      data_class: PII
      llm_visible: true
    why: invalid sensitive visibility
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="llm_visible.*PII"):
        load_rules(p)


def test_loader_llm_visible_true_with_compensation(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      target_in: [salary]
    then:
      data_class: COMPENSATION
      llm_visible: true
    why: invalid sensitive visibility
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="llm_visible.*COMPENSATION"):
        load_rules(p)


def test_loader_unknown_hit_policy(tmp_path: Path):
    doc = """
version: 1
hit_policy: all
rules:
  - id: rule1
    when:
      target_in: [name]
    then:
      data_class: PII
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="hit_policy"):
        load_rules(p)


def test_loader_invalid_version(tmp_path: Path):
    doc = """
version: 2
hit_policy: first
rules:
  - id: rule1
    when:
      target_in: [name]
    then:
      data_class: PII
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="version"):
        load_rules(p)


def test_loader_invalid_target_kind_in(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: rule1
    when:
      target_kind_in: [INVALID_KIND]
    then:
      data_class: PII
      llm_visible: false
    why: test
"""
    p = _write_yaml(tmp_path, doc)
    with pytest.raises(ClassificationRulesError, match="target_kind_in"):
        load_rules(p)


# --------------------------------------------------------------------------------------------------
# K1.1: Rule Evaluator Semantics (First-hit, AND, entity_in, target_kind_in, no-match)
# --------------------------------------------------------------------------------------------------


def test_evaluator_first_hit_order(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: first_rule
    when:
      target_in: [overlap_field]
    then:
      data_class: INTERNAL
      llm_visible: true
    why: first win
  - id: second_rule
    when:
      target_in: [overlap_field]
    then:
      data_class: PUBLIC
      llm_visible: true
    why: second ignored
"""
    p = _write_yaml(tmp_path, doc)
    rules = load_rules(p)
    res = rules.evaluate("overlap_field")
    assert res is not None
    assert res["rule_id"] == "first_rule"
    assert res["data_class"] == "INTERNAL"


def test_evaluator_and_of_conditions(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: employee_status
    when:
      entity_in: [EMPLOYEE]
      target_in: [status]
    then:
      data_class: INTERNAL
      llm_visible: true
    why: status of employee
"""
    p = _write_yaml(tmp_path, doc)
    rules = load_rules(p)

    # When entity matches, rule applies
    m1 = rules.evaluate("status", entity_type="EMPLOYEE")
    assert m1 is not None and m1["rule_id"] == "employee_status"

    # When entity does not match, rule does NOT apply
    m2 = rules.evaluate("status", entity_type="REQUISITION")
    assert m2 is None

    # When entity is omitted, rule does NOT apply
    m3 = rules.evaluate("status")
    assert m3 is None


def test_evaluator_entity_in_restricting_target_in(tmp_path: Path):
    doc = """
version: 1
hit_policy: first
rules:
  - id: claim_amount
    when:
      entity_in: [EXPENSE_CLAIM]
      target_in: [amount]
    then:
      data_class: COMPENSATION
      llm_visible: false
    why: expense reimbursement
  - id: default_amount
    when:
      target_in: [amount]
    then:
      data_class: INTERNAL
      llm_visible: false
    why: generic amount
"""
    p = _write_yaml(tmp_path, doc)
    rules = load_rules(p)

    claim = rules.evaluate("amount", entity_type="EXPENSE_CLAIM")
    assert claim is not None and claim["rule_id"] == "claim_amount"

    other = rules.evaluate("amount", entity_type="PURCHASE_ORDER")
    assert other is not None and other["rule_id"] == "default_amount"


def test_evaluator_target_kind_in_fallbacks():
    # Uses shipped rules
    # "pan_number" is not a canonical target, but identifier_kind recognizes it as PAN -> IDENTIFIER -> PII
    res_pan = suggest_classification("pan_number")
    assert res_pan is not None
    assert res_pan["data_class"] == "PII"
    assert res_pan["llm_visible"] is False

    # "bonus_amount" is not a canonical target, but is_compensation_key recognizes it as MONEY -> COMPENSATION
    res_money = suggest_classification("bonus_amount")
    assert res_money is not None
    assert res_money["data_class"] == "COMPENSATION"
    assert res_money["llm_visible"] is False


def test_evaluator_no_match_returns_none():
    res = suggest_classification("some_totally_arbitrary_unmapped_custom_metric")
    assert res is None


# --------------------------------------------------------------------------------------------------
# K1.2: Shipped YAML Content & Packaging
# --------------------------------------------------------------------------------------------------


def test_shipped_yaml_loads_and_is_cached():
    rules1 = load_rules()
    rules2 = load_rules()
    assert rules1 is rules2
    assert isinstance(rules1, ClassificationRules)
    assert rules1.version == 1
    assert rules1.hit_policy == "first"


def test_shipped_yaml_covers_all_four_data_classes():
    rules = load_rules()
    classes_found = set()
    for rule in rules.rules:
        classes_found.add(rule["then"]["data_class"])
    assert classes_found == set(DATA_CLASSES)  # PII, COMPENSATION, INTERNAL, PUBLIC


def test_shipped_yaml_llm_visible_false_for_all_pii_and_compensation():
    rules = load_rules()
    for rule in rules.rules:
        dc = rule["then"]["data_class"]
        lv = rule["then"]["llm_visible"]
        if dc in ("PII", "COMPENSATION"):
            assert lv is False, f"Rule {rule['id']} has sensitive class {dc} with llm_visible=True"


def test_shipped_yaml_canonical_model_classifications():
    # PII fields
    assert suggest_classification("full_name")["data_class"] == "PII"
    assert suggest_classification("email")["data_class"] == "PII"
    assert suggest_classification("phone")["data_class"] == "PII"
    assert suggest_classification("resume_text")["data_class"] == "PII"
    assert suggest_classification("resume_uri")["data_class"] == "PII"
    assert suggest_classification("hiring_manager_ref")["data_class"] == "PII"
    assert suggest_classification("employee_ref")["data_class"] == "PII"
    assert suggest_classification("assigned_to")["data_class"] == "PII"
    assert suggest_classification("reason")["data_class"] == "PII"

    # COMPENSATION fields
    assert suggest_classification("basic_salary")["data_class"] == "COMPENSATION"
    assert suggest_classification("gross_salary")["data_class"] == "COMPENSATION"
    assert suggest_classification("net_pay")["data_class"] == "COMPENSATION"
    assert suggest_classification("total_allowances")["data_class"] == "COMPENSATION"
    assert suggest_classification("total_deductions")["data_class"] == "COMPENSATION"
    assert suggest_classification("amount", entity_type="EXPENSE_CLAIM")["data_class"] == "COMPENSATION"

    # Descriptive fields (PUBLIC for REQUISITION, INTERNAL visible for others)
    for field in ("title", "location", "must_have_skills", "nice_to_have_skills"):
        sug = suggest_classification(field, entity_type="REQUISITION")
        assert sug is not None, f"Field {field} should have suggestion for REQUISITION"
        assert sug["data_class"] == "PUBLIC"
        assert sug["llm_visible"] is True

    for field in (
        "department", "designation", "status", "stage", "leave_type", "claim_type",
        "asset_type", "headcount", "skills", "currency", "month", "year", "fiscal_year",
        "min_years_experience", "max_years_experience"
    ):
        sug = suggest_classification(field)
        assert sug is not None, f"Field {field} should have suggestion"
        assert sug["data_class"] == "INTERNAL"
        assert sug["llm_visible"] is True, f"Field {field} should have llm_visible=True"

    # Other canonical fields (INTERNAL, llm_visible: false)
    for field in ("description", "years_experience", "joining_date", "punch_time"):
        sug = suggest_classification(field)
        assert sug is not None, f"Field {field} should have suggestion"
        assert sug["data_class"] == "INTERNAL"
        assert sug["llm_visible"] is False


# --------------------------------------------------------------------------------------------------
# K1.3: Synthesize suggested_classification & VALUE_CHECK
# --------------------------------------------------------------------------------------------------


def test_synthesize_adds_suggested_classification_to_coverage():
    discovered = DiscoveredEntity(
        name="job_requisition",
        fields={
            "id": DiscoveredField(name="id", path="id"),
            "job_title": DiscoveredField(name="job_title", path="job_title"),
            "dept": DiscoveredField(name="dept", path="dept"),
        },
    )
    mapping, report = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION, vendor="frappe")
    fields_cov = report.coverage["fields"]

    # title has descriptive suggestion
    assert "title" in fields_cov
    title_sug = fields_cov["title"]["suggested_classification"]
    assert title_sug is not None
    assert title_sug["data_class"] in ("PUBLIC", "INTERNAL")
    assert title_sug["llm_visible"] is True

    # hiring_manager_ref has PII suggestion
    assert "hiring_manager_ref" in fields_cov
    ref_sug = fields_cov["hiring_manager_ref"]["suggested_classification"]
    assert ref_sug is not None
    assert ref_sug["data_class"] == "PII"
    assert ref_sug["llm_visible"] is False


def test_synthesize_value_check_overrides_non_pii_with_pan(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    admin = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title"},
            {"name": "dept_pan_leak"},
        ],
        "sample_records": [
            {"id": "REQ-1", "title": "Software Lead", "dept_pan_leak": SAMPLE_PAN},
        ],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()

    fields_cov = body["coverage"]["fields"]
    # If dept_pan_leak was mapped to department (which is normally INTERNAL/visible),
    # the PAN sample value must trigger VALUE_CHECK overriding the non-PII suggestion
    dept_sug = fields_cov["department"]["suggested_classification"]
    assert dept_sug is not None
    assert dept_sug["data_class"] == "PII"
    assert dept_sug["llm_visible"] is False
    assert dept_sug["rule_id"] == "VALUE_CHECK"
    assert "PAN" in dept_sug["why"]

    # Sample value must NEVER appear anywhere in response body
    assert SAMPLE_PAN not in res.text

    # Stored version in mapping store must also not contain the sample value
    version = body["mapping_version"]
    stored_row = store._versions("acme", "frappe", "REQUISITION")[version]
    stored_json = json.dumps(stored_row)
    assert SAMPLE_PAN not in stored_json


def test_synthesize_value_check_overrides_with_email(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    admin = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title"},
            {"name": "contact_dept"},
        ],
        "sample_records": [
            {"id": "REQ-1", "title": "Developer", "contact_dept": SAMPLE_EMAIL},
        ],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()

    dept_sug = body["coverage"]["fields"]["department"]["suggested_classification"]
    assert dept_sug["rule_id"] == "VALUE_CHECK"
    assert dept_sug["data_class"] == "PII"
    assert "EMAIL" in dept_sug["why"]
    assert SAMPLE_EMAIL not in res.text


def test_synthesize_value_check_not_triggered_by_ordinary_text(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    admin = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title"},
            {"name": "department"},
        ],
        "sample_records": [
            {"id": "REQ-1", "title": "Developer", "department": ORDINARY_TEXT},
        ],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()

    dept_sug = body["coverage"]["fields"]["department"]["suggested_classification"]
    assert dept_sug["rule_id"] != "VALUE_CHECK"
    assert dept_sug["llm_visible"] is True


# --------------------------------------------------------------------------------------------------
# K1.4: Review Recomputation & Preserving VALUE_CHECK
# --------------------------------------------------------------------------------------------------


def test_review_preserves_value_check_when_source_unchanged():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(
            FieldMap(target="title", source="title"),
            FieldMap(target="department", source="dept_raw"),
        ),
    )
    cov = {
        "fields": {
            "title": {
                "status": "NEEDS_REVIEW",
                "source": "title",
                "suggested_classification": {
                    "data_class": "PUBLIC",
                    "llm_visible": True,
                    "rule_id": "public_descriptive",
                    "why": "Job title",
                },
            },
            "department": {
                "status": "NEEDS_REVIEW",
                "source": "dept_raw",
                "suggested_classification": {
                    "data_class": "PII",
                    "llm_visible": False,
                    "rule_id": "VALUE_CHECK",
                    "why": "sample values look like PAN",
                },
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 0, "needs_review": 2, "missing": 0, "unused": 0},
    }
    v1 = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    # Reviewer accepts both fields: department source is unchanged ("dept_raw")
    v2_row = store.review_version(
        "acme",
        "frappe",
        "REQUISITION",
        v1,
        reviewer="bob",
        actions={"accept": ["title", "department"]},
    )
    v2_cov = v2_row["coverage"]["fields"]

    # VALUE_CHECK suggestion must be retained
    dept_sug = v2_cov["department"]["suggested_classification"]
    assert dept_sug is not None
    assert dept_sug["rule_id"] == "VALUE_CHECK"
    assert dept_sug["data_class"] == "PII"

    # Non-VALUE_CHECK suggestion is recomputed
    title_sug = v2_cov["title"]["suggested_classification"]
    assert title_sug is not None
    assert title_sug["llm_visible"] is True


def test_review_drops_value_check_when_source_changed():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(FieldMap(target="department", source="dept_pan_leak"),),
    )
    cov = {
        "fields": {
            "department": {
                "status": "NEEDS_REVIEW",
                "source": "dept_pan_leak",
                "suggested_classification": {
                    "data_class": "PII",
                    "llm_visible": False,
                    "rule_id": "VALUE_CHECK",
                    "why": "sample values look like PAN",
                },
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 0, "needs_review": 1, "missing": 0, "unused": 0},
    }
    v1 = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    # Reviewer changes source of department to clean field "dept_clean"
    v2_row = store.review_version(
        "acme",
        "frappe",
        "REQUISITION",
        v1,
        reviewer="bob",
        actions={"manual": {"department": {"source": "dept_clean"}}},
    )
    v2_cov = v2_row["coverage"]["fields"]

    # Since source changed, VALUE_CHECK is NOT retained; recomputed from rules
    dept_sug = v2_cov["department"]["suggested_classification"]
    assert dept_sug is not None
    assert dept_sug["rule_id"] != "VALUE_CHECK"
    assert dept_sug["data_class"] in ("PUBLIC", "INTERNAL")
    assert dept_sug["llm_visible"] is True


# --------------------------------------------------------------------------------------------------
# K1.5: Approve accept_suggested_classification & classification_origin
# --------------------------------------------------------------------------------------------------


def test_approve_accept_suggested_true():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(
            FieldMap(target="title", source="title"),
            FieldMap(target="department", source="dept"),
        ),
    )
    cov = {
        "fields": {
            "title": {
                "status": "MATCHED",
                "source": "title",
                "suggested_classification": {
                    "data_class": "PUBLIC",
                    "llm_visible": True,
                    "rule_id": "public_descriptive",
                    "why": "Job title",
                },
            },
            "department": {
                "status": "MATCHED",
                "source": "dept",
                "suggested_classification": {
                    "data_class": "INTERNAL",
                    "llm_visible": True,
                    "rule_id": "internal_visible_descriptive",
                    "why": "Department",
                },
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 2, "needs_review": 0, "missing": 0, "unused": 0},
    }
    v = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    approved = store.approve_version(
        "acme",
        "frappe",
        "REQUISITION",
        v,
        approver="bob",
        accept_suggested_classification=True,
    )

    assert approved["status"] == "ACTIVE"
    assert approved["classification"]["title"] == {"data_class": "PUBLIC", "llm_visible": True}
    assert approved["classification"]["department"] == {"data_class": "INTERNAL", "llm_visible": True}

    assert approved["classification_origin"]["title"] == "SUGGESTED:public_descriptive"
    assert approved["classification_origin"]["department"] == "SUGGESTED:internal_visible_descriptive"


def test_approve_accept_suggested_false_and_absent():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(FieldMap(target="title", source="title"),),
    )
    cov = {
        "fields": {
            "title": {
                "status": "MATCHED",
                "source": "title",
                "suggested_classification": {
                    "data_class": "PUBLIC",
                    "llm_visible": True,
                    "rule_id": "public_descriptive",
                    "why": "Job title",
                },
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 1, "needs_review": 0, "missing": 0, "unused": 0},
    }
    v1 = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    # False -> defaults to PII, hidden
    app1 = store.approve_version(
        "acme",
        "frappe",
        "REQUISITION",
        v1,
        approver="bob",
        accept_suggested_classification=False,
    )
    assert app1["classification"]["title"] == {"data_class": "PII", "llm_visible": False}
    assert app1["classification_origin"]["title"] == "DEFAULT"

    # Absent -> same as False
    v2 = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)
    app2 = store.approve_version("acme", "frappe", "REQUISITION", v2, approver="carol")
    assert app2["classification"]["title"] == {"data_class": "PII", "llm_visible": False}
    assert app2["classification_origin"]["title"] == "DEFAULT"


def test_approve_explicit_wins_over_suggestion():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(
            FieldMap(target="title", source="title"),
            FieldMap(target="department", source="dept"),
        ),
    )
    cov = {
        "fields": {
            "title": {
                "status": "MATCHED",
                "source": "title",
                "suggested_classification": {
                    "data_class": "PUBLIC",
                    "llm_visible": True,
                    "rule_id": "public_descriptive",
                    "why": "Job title",
                },
            },
            "department": {
                "status": "MATCHED",
                "source": "dept",
                "suggested_classification": {
                    "data_class": "INTERNAL",
                    "llm_visible": True,
                    "rule_id": "internal_visible_descriptive",
                    "why": "Department",
                },
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 2, "needs_review": 0, "missing": 0, "unused": 0},
    }
    v = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    # Human explicitly classifies title as INTERNAL/True, overriding the suggestion PUBLIC/True
    explicit = {"title": {"data_class": "INTERNAL", "llm_visible": True}}
    approved = store.approve_version(
        "acme",
        "frappe",
        "REQUISITION",
        v,
        approver="bob",
        classification=explicit,
        accept_suggested_classification=True,
    )

    assert approved["classification"]["title"] == {"data_class": "INTERNAL", "llm_visible": True}
    assert approved["classification_origin"]["title"] == "MANUAL"

    # Department took the suggestion
    assert approved["classification"]["department"] == {"data_class": "INTERNAL", "llm_visible": True}
    assert approved["classification_origin"]["department"] == "SUGGESTED:internal_visible_descriptive"


def test_approve_field_without_suggestion_defaults():
    store = MappingStore(backend=MemoryKeyValueStore())
    m = EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="id",
        fields=(FieldMap(target="custom_no_rule", source="custom_raw"),),
    )
    cov = {
        "fields": {
            "custom_no_rule": {
                "status": "MATCHED",
                "source": "custom_raw",
                "suggested_classification": None,
            },
        },
        "unused_host_fields": [],
        "counts": {"matched": 1, "needs_review": 0, "missing": 0, "unused": 0},
    }
    v = store.propose_mapping("acme", m, proposed_by="alice", coverage=cov)

    approved = store.approve_version(
        "acme",
        "frappe",
        "REQUISITION",
        v,
        approver="bob",
        accept_suggested_classification=True,
    )

    assert approved["classification"]["custom_no_rule"] == {"data_class": "PII", "llm_visible": False}
    assert approved["classification_origin"]["custom_no_rule"] == "DEFAULT"


# --------------------------------------------------------------------------------------------------
# FastAPI TestClient API-level End-to-End Flow
# --------------------------------------------------------------------------------------------------


def test_http_synthesize_review_approve_flow(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()

    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")
    carol = bearer(roles=["platform_admin"], sub="carol")

    # 1. Synthesize with sample records carrying a PAN in department
    synth_payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title"},
            {"name": "department"},
        ],
        "sample_records": [
            {"id": "REQ-101", "title": "Senior Engineer", "department": SAMPLE_PAN},
        ],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=synth_payload, headers=alice)
    assert res.status_code == 200, res.text
    v1_body = res.json()
    v1 = v1_body["mapping_version"]

    # Verify VALUE_CHECK triggered and sample value is NOT in response
    dept_sug = v1_body["coverage"]["fields"]["department"]["suggested_classification"]
    assert dept_sug["rule_id"] == "VALUE_CHECK"
    assert dept_sug["data_class"] == "PII"
    assert SAMPLE_PAN not in res.text

    # 2. Review: Bob accepts title and department (sources unchanged)
    review_res = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v1}/review",
        json={"accept": ["title", "department"]},
        headers=bob,
    )
    assert review_res.status_code == 200, review_res.text
    v2_body = review_res.json()
    v2 = v2_body["version"]

    # In v2, VALUE_CHECK is preserved
    v2_dept_sug = v2_body["coverage"]["fields"]["department"]["suggested_classification"]
    assert v2_dept_sug["rule_id"] == "VALUE_CHECK"

    # 3. Approve: Carol approves with accept_suggested_classification: true
    approve_res = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v2}/approve",
        json={"accept_suggested_classification": True},
        headers=carol,
    )
    assert approve_res.status_code == 200, approve_res.text
    app_body = approve_res.json()

    assert app_body["status"] == "ACTIVE"
    assert app_body["classification"]["title"]["llm_visible"] is True
    assert app_body["classification"]["department"] == {"data_class": "PII", "llm_visible": False}

    assert app_body["classification_origin"]["title"].startswith("SUGGESTED:")
    assert app_body["classification_origin"]["department"] == "SUGGESTED:VALUE_CHECK"

    # 4. Check mappings listing
    listing = client.get("/v1/schema/mappings", headers=alice).json()
    active_row = next(r for r in listing["versions"] if r["version"] == v2)
    assert active_row["status"] == "ACTIVE"
    assert active_row["classification_origin"]["department"] == "SUGGESTED:VALUE_CHECK"


# --------------------------------------------------------------------------------------------------
# Follow-up Review Findings Tests
# --------------------------------------------------------------------------------------------------


def test_followup_requisition_title_is_public():
    sug = suggest_classification("title", entity_type="REQUISITION")
    assert sug is not None
    assert sug["data_class"] == "PUBLIC"
    assert sug["llm_visible"] is True
    assert sug["rule_id"] == "public_descriptive"


def test_followup_candidate_skills_is_internal_visible():
    sug = suggest_classification("skills", entity_type="CANDIDATE")
    assert sug is not None
    assert sug["data_class"] == "INTERNAL"
    assert sug["llm_visible"] is True
    assert sug["rule_id"] == "internal_visible_descriptive"


def test_followup_title_on_non_requisition_entity_not_public():
    sug_emp = suggest_classification("title", entity_type="EMPLOYEE")
    assert sug_emp is None or sug_emp["data_class"] != "PUBLIC"

    sug_cand = suggest_classification("title", entity_type="CANDIDATE")
    assert sug_cand is None or sug_cand["data_class"] != "PUBLIC"


def test_followup_rules_content_structure():
    rules = load_rules()
    rule_map = {r["id"]: r for r in rules.rules}

    pub = rule_map["public_descriptive"]
    assert pub["when"].get("entity_in") == ["REQUISITION"]
    assert "skills" not in pub["when"].get("target_in", [])

    internal_vis = rule_map["internal_visible_descriptive"]
    assert "skills" in internal_vis["when"].get("target_in", [])


def test_apply_value_check_pan_detected():
    fm = FieldMap(target="tax_code", source="pan_num")
    sample_records = [{"pan_num": SAMPLE_PAN}]
    coverage_fields = {
        "tax_code": {
            "suggested_classification": {
                "data_class": "INTERNAL",
                "llm_visible": True,
                "rule_id": "custom",
                "why": "custom",
            }
        }
    }
    apply_value_check([fm], sample_records, coverage_fields)
    assert coverage_fields["tax_code"]["suggested_classification"] == {
        "data_class": "PII",
        "llm_visible": False,
        "rule_id": "VALUE_CHECK",
        "why": "sample values look like PAN",
    }


def test_apply_value_check_email_inside_list():
    fm = FieldMap(target="contact_info", source="emails")
    sample_records = [{"emails": ["not-an-email", SAMPLE_EMAIL]}]
    coverage_fields = {"contact_info": {"suggested_classification": None}}
    apply_value_check([fm], sample_records, coverage_fields)
    assert coverage_fields["contact_info"]["suggested_classification"] == {
        "data_class": "PII",
        "llm_visible": False,
        "rule_id": "VALUE_CHECK",
        "why": "sample values look like EMAIL",
    }


def test_apply_value_check_plain_text_unchanged():
    fm = FieldMap(target="dept", source="raw_dept")
    sample_records = [{"raw_dept": ORDINARY_TEXT}]
    orig_sug = {"data_class": "INTERNAL", "llm_visible": True, "rule_id": "dept_rule", "why": "dept"}
    coverage_fields = {"dept": {"suggested_classification": dict(orig_sug)}}
    apply_value_check([fm], sample_records, coverage_fields)
    assert coverage_fields["dept"]["suggested_classification"] == orig_sug


def test_apply_value_check_bool_and_none_ignored():
    fm_bool = FieldMap(target="active", source="is_active")
    fm_none = FieldMap(target="remarks", source="notes")
    sample_records = [
        {"is_active": True, "notes": None},
        {"is_active": False, "notes": None},
    ]
    coverage_fields = {
        "active": {"suggested_classification": None},
        "remarks": {"suggested_classification": None},
    }
    apply_value_check([fm_bool, fm_none], sample_records, coverage_fields)
    assert coverage_fields["active"]["suggested_classification"] is None
    assert coverage_fields["remarks"]["suggested_classification"] is None


def test_apply_value_check_existing_pii_untouched():
    fm = FieldMap(target="employee_email", source="email")
    sample_records = [{"email": SAMPLE_EMAIL}]
    existing_pii_sug = {
        "data_class": "PII",
        "llm_visible": False,
        "rule_id": "pii_emails",
        "why": "Email address directly identifies an individual",
    }
    coverage_fields = {"employee_email": {"suggested_classification": dict(existing_pii_sug)}}
    apply_value_check([fm], sample_records, coverage_fields)
    assert coverage_fields["employee_email"]["suggested_classification"] == existing_pii_sug


def test_apply_value_check_uncovered_field_ignored():
    fm = FieldMap(target="uncovered", source="pan")
    sample_records = [{"pan": SAMPLE_PAN}]
    coverage_fields = {"other": {"suggested_classification": None}}
    apply_value_check([fm], sample_records, coverage_fields)
    assert "uncovered" not in coverage_fields
    assert coverage_fields["other"]["suggested_classification"] is None


def test_followup_single_source_of_truth_data_classes():
    import hrms_plugin.governance.classification_rules as rules_mod
    import hrms_plugin.storage.mapping_store as store_mod

    assert store_mod.DATA_CLASSES is rules_mod.DATA_CLASSES
    assert store_mod.SENSITIVE_CLASSES is rules_mod.SENSITIVE_CLASSES
    assert store_mod.DATA_CLASSES == ("PII", "COMPENSATION", "INTERNAL", "PUBLIC")
    assert store_mod.SENSITIVE_CLASSES == ("PII", "COMPENSATION")

