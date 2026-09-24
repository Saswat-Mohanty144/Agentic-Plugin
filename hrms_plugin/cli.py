"""Command-Line Interface (CLI) for the Standalone Agentic HRMS AI Plugin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase
from hrms_plugin.schema.introspector import SchemaIntrospector


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrms-plugin",
        description="Autonomous Agentic HRMS Intelligence Microservice & Setup CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 1. Introspect Schema
    intro_parser = subparsers.add_parser("introspect", help="Introspect Swagger 2.0 / OpenAPI schema")
    intro_parser.add_argument("schema_path", help="Path or URL to swagger.json or openapi.yaml")

    # 2. Calculate EOSB
    eosb_parser = subparsers.add_parser("calculate-eosb", help="Calculate UAE Gratuity (EOSB) under Article 51")
    eosb_parser.add_argument("--basic", type=float, required=True, help="Monthly basic wage in AED")
    eosb_parser.add_argument("--tenure", type=float, required=True, help="Completed years of service")

    # 3. Calculate Salary Structure
    sal_parser = subparsers.add_parser("calculate-salary", help="Calculate Gross-to-Net CTC Structuring")
    sal_parser.add_argument("--ctc", type=float, required=True, help="Annual CTC or Gross Amount")
    sal_parser.add_argument("--jurisdiction", default="IN", choices=["IN", "AE"], help="Jurisdiction code")

    # 4. Audit Compliance
    audit_parser = subparsers.add_parser("audit-compliance", help="Audit employee records for labor law violations")
    audit_parser.add_argument("--file", help="Path to JSON file containing list of employee records")
    audit_parser.add_argument("--jurisdiction", default="AE", choices=["IN", "AE", "SA", "US"], help="Jurisdiction")

    # 5. Check Bias
    bias_parser = subparsers.add_parser("check-bias", help="Audit job description for exclusionary wording")
    bias_parser.add_argument("--text", required=True, help="Job description text")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    kb = StatutoryKnowledgeBase()

    if args.command == "introspect":
        path = Path(args.schema_path)
        if not path.exists():
            print(f"Error: Schema file '{args.schema_path}' not found.", file=sys.stderr)
            return 1
        res = SchemaIntrospector.introspect_file(path)
        print("=== Introspection Succeeded ===")
        print(f"Title: {res.title}")
        print(f"Version: {res.version}")
        print(f"Endpoints: {res.endpoints_count}")
        print(f"Entities Discovered: {list(res.entities.keys())}")
        return 0

    elif args.command == "calculate-eosb":
        payroll = StatutoryPayrollAgent(kb=kb)
        res = payroll.calculate_uae_eosb(basic_wage_monthly=args.basic, tenure_years=args.tenure)
        print("=== UAE Statutory EOSB Calculation (Article 51) ===")
        print(f"Basic Wage: AED {res.basic_wage_monthly:,.2f}")
        print(f"Tenure: {res.tenure_years} years")
        print(f"Gratuity Payable: AED {res.total_eosb_gratuity:,.2f}")
        print(f"Citation: {res.statutory_citation}")
        return 0

    elif args.command == "calculate-salary":
        payroll = StatutoryPayrollAgent(kb=kb)
        if args.jurisdiction == "AE":
            res_ae = payroll.structure_uae_salary(monthly_gross=args.ctc / 12.0)
            print("=== UAE Monthly Salary Structuring ===")
            print(f"Monthly Gross: AED {res_ae.monthly_gross:,.2f}")
            print(f"Basic Wage: AED {res_ae.basic_wage:,.2f}")
            print(f"Housing Allowance: AED {res_ae.housing_allowance:,.2f}")
        else:
            res_in = payroll.structure_india_salary(annual_ctc=args.ctc)
            print("=== India CTC Breakdown ===")
            print(f"Annual CTC: INR {res_in.annual_ctc:,.2f}")
            print(f"Monthly Gross: INR {res_in.gross_salary:,.2f}")
            print(f"Employee EPF (12%): INR {res_in.employee_pf:,.2f}")
            print(f"Net Monthly Take-Home: INR {res_in.net_take_home:,.2f}")
        return 0


    elif args.command == "audit-compliance":
        comp = ComplianceAgent(kb=kb)
        employees = []
        if args.file:
            path = Path(args.file)
            if path.exists():
                employees = json.loads(path.read_text(encoding="utf-8"))
        else:
            # Demo record
            employees = [{"id": "DEMO-01", "probation_days": 210, "weekly_hours": 52, "uae_wps_registered": False}]

        report = comp.audit_employees(
            tenant_id="CLI_ADMIN",
            jurisdiction=Jurisdiction(args.jurisdiction),
            employees=employees,
        )
        print(f"=== Statutory Labor Audit Report [{args.jurisdiction}] ===")
        print(f"Status: {'COMPLIANT' if report.is_compliant else 'NON-COMPLIANT'}")
        print(f"Violations Detected: {report.violations_count}")
        for v in report.violations:
            print(f"- [{v.severity}] {v.rule_name}: {v.description}")
            print(f"  Citation: {v.statutory_citation}")
        return 0

    elif args.command == "check-bias":
        rec = RecruitmentAgent()
        b_res = rec.audit_job_description_bias(args.text)
        print("=== Recruitment Bias & Inclusivity Audit ===")
        print(f"Biased Terms Found: {len(b_res.detected_terms)}")
        if b_res.detected_terms:
            for item in b_res.detected_terms:
                flagged = item.get("flagged_term", "")
                repl = item.get("replacement_recommendation", "")
                print(f"- Flagged: '{flagged}' -> Replace: '{repl}'")
            print(f"\nInclusive Rewritten JD:\n{b_res.suggested_rewrite}")

        else:
            print("No exclusionary terms detected. The job description is gender-neutral and EEO compliant.")
        return 0


    return 0


if __name__ == "__main__":
    sys.exit(main())
