"""Idempotent seed script for real Frappe HRMS sandbox in Docker.

Seeds:
- Caddy TLS CA bundle
- ERPNext Setup Wizard completion (Acme Test Corp, India, INR)
- Dedicated API user (api_worker) with roles and API key/secret
- Salary components (B, DA, HRA)
- Salary structures and assignments
- Two employees: one below 15000 ceiling, one above
- Submitted salary slips for 2026-10, draft slip (2026-11), cancelled slip (2026-09)
- >= 3 Job Requisitions with different statuses (including unmapped 'Archived' status)
  and > 100 total requisitions to test multi-page offset paging.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

import certifi

COMPOSE_FILE = "/Users/satosh/frappe-hrms-sandbox/docker-compose.yml"
CA_BUNDLE_PATH = "/tmp/frappe_sandbox_ca.crt"


def build_ca_bundle() -> str:
    """Build a CA bundle combining certifi and Caddy internal root CA."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "caddy", "cat",
           "/data/caddy/pki/authorities/local/root.crt"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    caddy_root = proc.stdout.strip()
    if not caddy_root or "BEGIN CERTIFICATE" not in caddy_root:
        raise RuntimeError("Failed to retrieve Caddy root CA certificate")

    certifi_certs = Path(certifi.where()).read_text(encoding="utf-8")
    combined = f"{certifi_certs}\n\n# Caddy local authority root CA\n{caddy_root}\n"
    bundle_path = Path(CA_BUNDLE_PATH)
    bundle_path.write_text(combined, encoding="utf-8")
    return str(bundle_path)


_SEED_SCRIPT = """
import json
import time
import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.utils.password import set_encrypted_password, get_decrypted_password

frappe.init('hrms.localhost')
frappe.connect()

company_name = "Acme Test Corp"
company_abbr = "ATC"

# 1. Setup Wizard if Company does not exist
if not frappe.get_list("Company"):
    setup_complete({
        "currency": "INR",
        "full_name": "Administrator",
        "company_name": company_name,
        "timezone": "Asia/Kolkata",
        "company_abbr": company_abbr,
        "industry": "Services",
        "country": "India",
        "fy_start_date": "2026-04-01",
        "fy_end_date": "2027-03-31",
        "language": "english",
        "company_tagline": "Testing",
        "chart_of_accounts": "Standard",
    })
    frappe.db.commit()

# 2. Holiday list for Company
hl_name = "Test Holidays 2026"
if not frappe.db.exists("Holiday List", hl_name):
    hl = frappe.new_doc("Holiday List")
    hl.holiday_list_name = hl_name
    hl.from_date = "2026-01-01"
    hl.to_date = "2026-12-31"
    hl.insert(ignore_permissions=True)
frappe.db.set_value("Company", company_name, "default_holiday_list", hl_name)
frappe.db.commit()

# 3. Dedicated API user
api_username = "api_worker"
if not frappe.db.exists("User", api_username):
    frappe.db.sql(\"\"\"
        INSERT INTO tabUser (name, email, first_name, enabled, docstatus, user_type)
        VALUES (%s, %s, 'API Worker', 1, 0, 'System User')
    \"\"\", (api_username, api_username))
    frappe.db.commit()

for role in ["HR Manager", "HR User", "System Manager"]:
    if not frappe.db.exists("Has Role", {"parent": api_username, "role": role}):
        frappe.db.sql(\"\"\"
            INSERT INTO `tabHas Role` (name, parent, parenttype, parentfield, role)
            VALUES (%s, %s, 'User', 'roles', %s)
        \"\"\", (frappe.generate_hash(length=10), api_username, role))
frappe.db.commit()

user_doc = frappe.get_doc("User", api_username)
api_key = user_doc.api_key
api_secret = get_decrypted_password("User", api_username, fieldname="api_secret", raise_exception=False)
if not api_key or not api_secret:
    api_key = frappe.generate_hash(length=15)
    api_secret = frappe.generate_hash(length=15)
    frappe.db.set_value("User", api_username, "api_key", api_key)
    set_encrypted_password("User", api_username, api_secret, fieldname="api_secret")
    frappe.db.commit()

# 4. Designation
if not frappe.db.exists("Designation", "Engineer"):
    d = frappe.new_doc("Designation")
    d.designation_name = "Engineer"
    d.insert(ignore_permissions=True)
    frappe.db.commit()

# 5. Salary Components
components = [
    ("Basic", "B", "Earning"),
    ("Dearness Allowance", "DA", "Earning"),
    ("House Rent Allowance", "HRA", "Earning"),
]
for sc_name, abbr, sc_type in components:
    if not frappe.db.exists("Salary Component", sc_name):
        sc = frappe.new_doc("Salary Component")
        sc.salary_component = sc_name
        sc.salary_component_abbr = abbr
        sc.type = sc_type
        sc.insert(ignore_permissions=True)
frappe.db.commit()

# 6. Salary Structures
struct_std = "Standard Salary Structure"
if not frappe.db.exists("Salary Structure", struct_std):
    ss1 = frappe.new_doc("Salary Structure")
    ss1.name = struct_std
    ss1.company = company_name
    ss1.currency = "INR"
    ss1.is_active = "Yes"
    ss1.payroll_frequency = "Monthly"
    ss1.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 10000.0})
    ss1.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 2000.0})
    ss1.insert(ignore_permissions=True)
    ss1.submit()
    frappe.db.commit()

struct_exec = "Executive Salary Structure"
if not frappe.db.exists("Salary Structure", struct_exec):
    ss2 = frappe.new_doc("Salary Structure")
    ss2.name = struct_exec
    ss2.company = company_name
    ss2.currency = "INR"
    ss2.is_active = "Yes"
    ss2.payroll_frequency = "Monthly"
    ss2.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 20000.0})
    ss2.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 5000.0})
    ss2.insert(ignore_permissions=True)
    ss2.submit()
    frappe.db.commit()

# 7. Employees
# Employee 1: Below ceiling (wage = 12000 < 15000)
if not frappe.db.exists("Employee", {"first_name": "TestEmployeeOne"}):
    emp1 = frappe.new_doc("Employee")
    emp1.first_name = "TestEmployeeOne"
    emp1.gender = "Male"
    emp1.date_of_birth = "1990-01-01"
    emp1.date_of_joining = "2020-01-01"
    emp1.status = "Active"
    emp1.company = company_name
    emp1.designation = "Engineer"
    emp1.notice_number_of_days = 30
    emp1.insert(ignore_permissions=True)
    emp1_id = emp1.name
else:
    emp1_id = frappe.db.get_value("Employee", {"first_name": "TestEmployeeOne"}, "name")

# Employee 2: Above ceiling (wage = 25000 > 15000)
if not frappe.db.exists("Employee", {"first_name": "TestEmployeeTwo"}):
    emp2 = frappe.new_doc("Employee")
    emp2.first_name = "TestEmployeeTwo"
    emp2.gender = "Female"
    emp2.date_of_birth = "1992-02-02"
    emp2.date_of_joining = "2020-01-01"
    emp2.status = "Active"
    emp2.company = company_name
    emp2.designation = "Engineer"
    emp2.notice_number_of_days = 45
    emp2.insert(ignore_permissions=True)
    emp2_id = emp2.name
else:
    emp2_id = frappe.db.get_value("Employee", {"first_name": "TestEmployeeTwo"}, "name")
frappe.db.commit()

# Salary structure assignments
if not frappe.db.exists("Salary Structure Assignment", {"employee": emp1_id, "docstatus": 1}):
    ssa1 = frappe.new_doc("Salary Structure Assignment")
    ssa1.employee = emp1_id
    ssa1.salary_structure = struct_std
    ssa1.from_date = "2026-01-01"
    ssa1.company = company_name
    ssa1.currency = "INR"
    ssa1.base = 12000.0
    ssa1.insert(ignore_permissions=True)
    ssa1.submit()
    frappe.db.commit()

if not frappe.db.exists("Salary Structure Assignment", {"employee": emp2_id, "docstatus": 1}):
    ssa2 = frappe.new_doc("Salary Structure Assignment")
    ssa2.employee = emp2_id
    ssa2.salary_structure = struct_exec
    ssa2.from_date = "2026-01-01"
    ssa2.company = company_name
    ssa2.currency = "INR"
    ssa2.base = 25000.0
    ssa2.insert(ignore_permissions=True)
    ssa2.submit()
    frappe.db.commit()

# 8. Salary Slips
# 8a. Employee 1 submitted slip (2026-10)
slip1_id = frappe.db.get_value("Salary Slip", {"employee": emp1_id, "start_date": "2026-10-01", "docstatus": 1})
if not slip1_id:
    slip1 = frappe.new_doc("Salary Slip")
    slip1.employee = emp1_id
    slip1.employee_name = "TestEmployeeOne"
    slip1.salary_structure = struct_std
    slip1.company = company_name
    slip1.posting_date = "2026-10-31"
    slip1.start_date = "2026-10-01"
    slip1.end_date = "2026-10-31"
    slip1.currency = "INR"
    slip1.exchange_rate = 1.0
    slip1.payroll_frequency = "Monthly"
    slip1.total_working_days = 31
    slip1.payment_days = 31
    slip1.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 10000.0})
    slip1.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 2000.0})
    slip1.gross_pay = 12000.0
    slip1.net_pay = 12000.0
    slip1.insert(ignore_permissions=True)
    slip1.submit()
    frappe.db.commit()
    slip1_id = slip1.name

# 8b. Employee 2 submitted slip (2026-10)
slip2_id = frappe.db.get_value("Salary Slip", {"employee": emp2_id, "start_date": "2026-10-01", "docstatus": 1})
if not slip2_id:
    slip2 = frappe.new_doc("Salary Slip")
    slip2.employee = emp2_id
    slip2.employee_name = "TestEmployeeTwo"
    slip2.salary_structure = struct_exec
    slip2.company = company_name
    slip2.posting_date = "2026-10-31"
    slip2.start_date = "2026-10-01"
    slip2.end_date = "2026-10-31"
    slip2.currency = "INR"
    slip2.exchange_rate = 1.0
    slip2.payroll_frequency = "Monthly"
    slip2.total_working_days = 31
    slip2.payment_days = 31
    slip2.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 20000.0})
    slip2.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 5000.0})
    slip2.gross_pay = 25000.0
    slip2.net_pay = 25000.0
    slip2.insert(ignore_permissions=True)
    slip2.submit()
    frappe.db.commit()
    slip2_id = slip2.name

# 8c. Draft slip (2026-11) for Employee 1 (docstatus: 0)
draft_id = frappe.db.get_value("Salary Slip", {"employee": emp1_id, "start_date": "2026-11-01", "docstatus": 0})
if not draft_id:
    dslip = frappe.new_doc("Salary Slip")
    dslip.employee = emp1_id
    dslip.employee_name = "TestEmployeeOne"
    dslip.salary_structure = struct_std
    dslip.company = company_name
    dslip.posting_date = "2026-11-30"
    dslip.start_date = "2026-11-01"
    dslip.end_date = "2026-11-30"
    dslip.currency = "INR"
    dslip.exchange_rate = 1.0
    dslip.payroll_frequency = "Monthly"
    dslip.total_working_days = 30
    dslip.payment_days = 30
    dslip.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 10000.0})
    dslip.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 2000.0})
    dslip.gross_pay = 12000.0
    dslip.net_pay = 12000.0
    dslip.insert(ignore_permissions=True)
    frappe.db.commit()
    draft_id = dslip.name

# 8d. Cancelled slip (2026-09) for Employee 1 (docstatus: 2)
cancelled_id = frappe.db.get_value("Salary Slip", {"employee": emp1_id, "start_date": "2026-09-01", "docstatus": 2})
if not cancelled_id:
    cslip = frappe.new_doc("Salary Slip")
    cslip.employee = emp1_id
    cslip.employee_name = "TestEmployeeOne"
    cslip.salary_structure = struct_std
    cslip.company = company_name
    cslip.posting_date = "2026-09-30"
    cslip.start_date = "2026-09-01"
    cslip.end_date = "2026-09-30"
    cslip.currency = "INR"
    cslip.exchange_rate = 1.0
    cslip.payroll_frequency = "Monthly"
    cslip.total_working_days = 30
    cslip.payment_days = 30
    cslip.append("earnings", {"salary_component": "Basic", "abbr": "B", "amount": 10000.0})
    cslip.append("earnings", {"salary_component": "Dearness Allowance", "abbr": "DA", "amount": 2000.0})
    cslip.gross_pay = 12000.0
    cslip.net_pay = 12000.0
    cslip.insert(ignore_permissions=True)
    cslip.submit()
    cslip.cancel()
    frappe.db.commit()
    cancelled_id = cslip.name

# 9. Job Requisitions (seed 105 to verify offset paging > 1 page)
# Include standard mapped statuses: Pending, Open & Approved, Rejected, Filled, On Hold, Cancelled
# and one unmapped status: Archived
all_statuses = ["Pending", "Open & Approved", "Rejected", "Filled", "On Hold", "Cancelled"]
req_values = []
for i in range(1, 106):
    req_name = f"HR-HIREQ-TEST-{i:04d}"
    if i == 3:
        status = "Archived"  # unmapped status
    else:
        status = all_statuses[(i - 1) % len(all_statuses)]
    req_values.append((
        req_name, "Engineer", 2, company_name, status, emp1_id, "2026-10-01",
        f"Requisition description for position {i}", "Administrator", "Administrator"
    ))

frappe.db.sql(\"\"\"
    INSERT INTO `tabJob Requisition`
    (name, designation, no_of_positions, company, status, requested_by, posting_date, description, owner, modified_by)
    VALUES \"\"\" + ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(req_values)) + \"\"\"
    ON DUPLICATE KEY UPDATE status=VALUES(status)
\"\"\", [v for row in req_values for v in row])
frappe.db.commit()

out = {
    "company": company_name,
    "api_user": api_username,
    "api_key": api_key,
    "api_secret": api_secret,
    "employee_below_ceiling": emp1_id,
    "employee_above_ceiling": emp2_id,
    "salary_structure_below": struct_std,
    "salary_structure_above": struct_exec,
    "slip_below_ceiling": slip1_id,
    "slip_above_ceiling": slip2_id,
    "draft_slip": draft_id,
    "cancelled_slip": cancelled_id,
    "job_requisitions_count": frappe.db.count("Job Requisition"),
    "unmapped_requisition_id": "HR-HIREQ-TEST-0003",
}
print("__SEED_RESULT__" + json.dumps(out))
"""


def seed_frappe() -> Dict[str, Any]:
    """Execute the idempotent Frappe HRMS seeding process."""
    ca_bundle = build_ca_bundle()

    cmd = [
        "docker", "compose", "-f", COMPOSE_FILE, "exec",
        "-w", "/home/frappe/frappe-bench/sites",
        "-T", "frappe",
        "../env/bin/python",
    ]
    proc = subprocess.run(cmd, input=_SEED_SCRIPT, capture_output=True, text=True, check=True)
    marker = "__SEED_RESULT__"
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            data = json.loads(line[len(marker):])
            data["ca_bundle"] = ca_bundle
            data["base_url"] = "https://localhost:8443"
            return data

    raise RuntimeError(f"Seed script did not return result marker. stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def main() -> None:
    result = seed_frappe()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
