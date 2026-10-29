"""Coverage tests for hrms_plugin/cli.py."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from hrms_plugin.cli import main


def test_cli_no_args_prints_help(capsys: pytest.CaptureFixture[str]):
    """Positive test: Running CLI without arguments displays help message and exits 0."""
    code = main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "Autonomous Agentic HRMS Intelligence Microservice & Setup CLI" in captured.out
    assert "Available commands" in captured.out


def test_cli_introspect_file_not_found(capsys: pytest.CaptureFixture[str]):
    """Negative test: Introspecting a non-existent file prints error to stderr and exits 1."""
    code = main(["introspect", "/tmp/non_existent_openapi_spec_12345.json"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Error: Schema file '/tmp/non_existent_openapi_spec_12345.json' not found." in captured.err


def test_cli_introspect_valid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Positive test: Introspecting a valid OpenAPI schema file succeeds and prints summary."""
    schema_file = tmp_path / "openapi.json"
    schema_data = {
        "openapi": "3.0.0",
        "info": {"title": "Test HRMS Spec", "version": "2.4.0"},
        "paths": {
            "/api/v1/employees": {
                "get": {"summary": "List employees", "responses": {"200": {"description": "OK"}}},
                "post": {"summary": "Create employee", "responses": {"201": {"description": "Created"}}},
            }
        },
    }
    schema_file.write_text(json.dumps(schema_data), encoding="utf-8")

    code = main(["introspect", str(schema_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== Introspection Succeeded ===" in captured.out
    assert "Title: Test HRMS Spec" in captured.out
    assert "Version: 2.4.0" in captured.out
    assert "Endpoints: 1" in captured.out


def test_cli_calculate_eosb(capsys: pytest.CaptureFixture[str]):
    """Positive test: Calculate UAE EOSB gratuity."""
    code = main(["calculate-eosb", "--basic", "10000", "--tenure", "5"])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== UAE Statutory EOSB Calculation (Article 51) ===" in captured.out
    assert "Basic Wage: AED 10,000.00" in captured.out
    assert "Tenure: 5.0 years" in captured.out
    assert "Gratuity Payable: AED 35,000.00" in captured.out


def test_cli_calculate_salary_uae(capsys: pytest.CaptureFixture[str]):
    """Positive test: Calculate UAE salary structure."""
    code = main(["calculate-salary", "--ctc", "120000", "--jurisdiction", "AE"])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== UAE Monthly Salary Structuring ===" in captured.out
    assert "Monthly Gross: AED 10,000.00" in captured.out
    assert "Basic Wage: AED 6,000.00" in captured.out
    assert "Housing Allowance: AED 3,000.00" in captured.out


def test_cli_calculate_salary_india(capsys: pytest.CaptureFixture[str]):
    """Positive test: Calculate India salary structure."""
    code = main(["calculate-salary", "--ctc", "1200000", "--jurisdiction", "IN"])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== India CTC Breakdown ===" in captured.out
    assert "Annual CTC: INR 1,200,000.00" in captured.out
    assert "Monthly Gross: INR 93,276.92" in captured.out


def test_cli_audit_compliance_reports_violations(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Fixed (agy coverage review): violations print rule_key and summary instead of crashing on rule_name."""
    rec_file = tmp_path / "employees.json"
    rec_file.write_text(json.dumps([{"id": "E1", "probation_days": 210, "weekly_hours": 52,
                                     "uae_wps_registered": False}]), encoding="utf-8")
    code = main(["audit-compliance", "--jurisdiction", "AE", "--file", str(rec_file)])
    out = capsys.readouterr().out
    assert code == 0 and "Status: NON-COMPLIANT" in out
    assert "probation" in out.lower() and "Citation:" in out


def test_cli_audit_compliance_requires_a_file(capsys: pytest.CaptureFixture[str]):
    """No built-in demo record: without --file the CLI refuses instead of auditing invented data."""
    assert main(["audit-compliance", "--jurisdiction", "AE"]) == 2
    assert "--file" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "not json", '{"id": "E1"}'])
def test_cli_audit_compliance_never_reports_compliant_without_records(tmp_path, capsys, content):
    """A missing or unreadable file used to print 'Status: COMPLIANT' for zero records."""
    path = tmp_path / "employees.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert main(["audit-compliance", "--jurisdiction", "AE", "--file", str(path)]) == 2
    captured = capsys.readouterr()
    assert "COMPLIANT" not in captured.out and captured.err


def test_cli_audit_compliance_file_custom(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Positive test: Audit compliance from external JSON file with compliant records."""
    rec_file = tmp_path / "employees.json"
    records = [{"id": "E100", "probation_days": 90, "weekly_hours": 40, "uae_wps_registered": True}]
    rec_file.write_text(json.dumps(records), encoding="utf-8")

    code = main(["audit-compliance", "--jurisdiction", "AE", "--file", str(rec_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== Statutory Labor Audit Report [AE] ===" in captured.out
    assert "Status: COMPLIANT" in captured.out
    assert "Violations Detected: 0" in captured.out




def test_cli_check_bias_flagged_terms(capsys: pytest.CaptureFixture[str]):
    """Positive test: Biased terms in JD are identified and replacement suggested."""
    text = "We are seeking an aggressive rockstar ninja developer to dominate our sprint goals."
    code = main(["check-bias", "--text", text])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== Recruitment Bias & Inclusivity Audit ===" in captured.out
    assert "Biased Terms Found:" in captured.out
    assert "Flagged:" in captured.out
    assert "Inclusive Rewritten JD:" in captured.out


def test_cli_check_bias_neutral_text(capsys: pytest.CaptureFixture[str]):
    """Boundary test: Neutral job description outputs EEO compliant message."""
    text = "We are looking for a software engineer proficient in Python and database management."
    code = main(["check-bias", "--text", text])
    assert code == 0
    captured = capsys.readouterr()
    assert "=== Recruitment Bias & Inclusivity Audit ===" in captured.out
    assert "No exclusionary terms detected. The job description is gender-neutral and EEO compliant." in captured.out


def test_cli_module_main_invocation(monkeypatch: pytest.MonkeyPatch):
    """Positive test: Invoking hrms_plugin.cli as __main__ triggers sys.exit(0)."""
    monkeypatch.setattr(sys, "argv", ["hrms-plugin"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("hrms_plugin.cli", run_name="__main__")
    assert excinfo.value.code == 0
