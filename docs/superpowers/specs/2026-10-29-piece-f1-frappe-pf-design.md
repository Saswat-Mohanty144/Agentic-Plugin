# Design: Piece F1 — Frappe HRMS payroll read path and PF calculation

* **Date**: 2026-10-29 · **Status**: decided by architect (HR domain + AI architecture) under user delegation
* **Answers**: Q1 (CTC vs salary → which value feeds PF), Q2 (payload changes must not produce wrong calculations), Q6 (vendor translation is configuration plus a thin adapter, not LLM code)
* **Rules**: strict TDD; positive, negative and boundary tests for every behaviour; no assumptions.

## 1. Facts this design rests on (verified 2026-10-29)
- The vendored iCeipts spec has **no employee or payroll endpoints** (login, department, skills and requisitions only). → `NEEDS_HUMAN: iCeipts payroll API spec`. F1 therefore uses Frappe HRMS.
- Frappe HRMS (`frappe/hrms`, develop branch):
  - **Salary Slip**: `employee`, `employee_name`, `start_date`, `end_date`, `currency`, `gross_pay`, `net_pay`, `total_deduction`, `earnings`/`deductions` (tables of Salary Detail), `status` (Draft / Submitted / Cancelled / Withheld).
  - **Salary Detail**: `salary_component`, `abbr`, `amount`, `statistical_component`, `do_not_include_in_total`.
  - **Salary Structure Assignment**: `base`, `ctc`, `annual_gross_earning`, i.e. CTC, gross and base are *different* numbers.
- Frappe REST (docs.frappe.io):
  - auth header `Authorization: token api_key:api_secret`;
  - lists are `GET /api/resource/:doctype` with JSON `fields`/`filters` and the result under `data`;
  - a single document is `GET /api/resource/:doctype/:name` with the result under `data`;
  - the default page size is 20 (`limit_page_length`).

## 2. Decisions

| # | Decision |
|---|---|
| F1-1 | **The PF wage is computed from the submitted Salary Slip's earnings**, never from "salary" or CTC. Which earnings count is a **per-tenant payroll rule**: `required_components` (e.g. Basic `B`) and `optional_components` (e.g. DA). Component names and abbreviations are configured per customer in Frappe, so they can't be hard-coded. (Q1) |
| F1-2 | Tool `calculate_pf_contribution(employee_id, period)`. The LLM supplies **only** a record ID and a month (`YYYY-MM`), never an amount. `tenant_id` and `jurisdiction` are injected by the server. Permission `VIEW_SALARY`; intent `PAYROLL_STRUCTURE`. Jurisdiction other than `IN` → `NOT_APPLICABLE`. |
| F1-3 | Host connection registry `HRMS_HOSTS` (YAML, strict, per tenant): `vendor: frappe`, `base_url` (https), `api_key_env`, `api_secret_env` (names of environment variables; **secrets never in the file**), `timeout_seconds` 1–60. Missing file, entry or env var → `HOST_NOT_CONFIGURED` (fail closed). Real values: **NEEDS_HUMAN**. |
| F1-4 | Payroll rules registry `HRMS_PAYROLL_RULES` (YAML, strict, per tenant): `rules_version`, `currency`, and `pf` with `required_components` (non-empty), `optional_components`, `employee_rate`, `employer_rate`, `eps_rate`, `wage_ceiling`, `eps_wage_ceiling` (decimal strings), `apply_ceiling_to_employee`, `apply_ceiling_to_employer`. Missing → `RULES_NOT_CONFIGURED`. The test fixture uses the statutory EPF values (12% / 12%, EPS 8.33%, ₹15,000 ceiling); **production values: NEEDS_HUMAN** (payroll / legal confirm per customer). |
| F1-5 | **Fetch**: list `Salary Slip` with filters `employee =`, `start_date` within the month and `docstatus = 1` (submitted only), `limit_page_length 2`. 0 slips → `SLIP_NOT_FOUND`; 2 → `AMBIGUOUS_SLIPS`; exactly 1 → fetch that document. HTTP or transport failure → `HOST_ERROR` (no retry of state; GET only). |
| F1-6 | **Schema fingerprint (Q2)**: SHA-256 over the sorted `path:json-type` pairs of every field the calculation reads (top level, plus the union over `earnings[]`). The approved fingerprint is stored per `(tenant, vendor, doctype)`. No approved fingerprint → `MAPPING_NOT_APPROVED` (returns the observed fingerprint for an admin). Mismatch → `SCHEMA_DRIFT`: no calculation, and a quarantine record with document name, observed and expected fingerprints and time, **never the payload**. |
| F1-7 | Approval endpoint `POST /v1/schema/fingerprints/approve` {vendor, doctype, fingerprint}: `MANAGE_SCHEMA` and a token issued ≤ 300 s ago (same freshness rule as remediation); `approved_by` = token `sub`. `GET /v1/schema/fingerprints/quarantine` (`MANAGE_SCHEMA`) lists the tenant's quarantine. Full approval workflow is piece G. |
| F1-8 | **Validation (fail closed, `INCONSISTENT_PAYLOAD`)**: currency equals the rule currency; every amount numeric and ≥ 0; no duplicate component `abbr`; the sum of earnings excluding `statistical_component` and `do_not_include_in_total` equals `gross_pay` within ₹0.01. A missing **required** component → `PF_COMPONENT_MISSING`; a missing optional one counts as 0. |
| F1-9 | **Computation** in `Decimal`, rounded ROUND_HALF_UP to whole rupees: <ul><li>`pf_wage` = Σ configured components</li><li>employee contribution = round(rate × (min(wage, ceiling) if `apply_ceiling_to_employee` else wage))</li><li>employer total = same rule with `employer_rate` / `apply_ceiling_to_employer`</li><li>EPS = round(`eps_rate` × min(wage, `eps_wage_ceiling`)), always capped</li><li>employer EPF = employer total − EPS</li></ul> Amounts are returned as **decimal strings**. |
| F1-10 | **Provenance**: the result carries vendor, doctype, document name, `modified`, fingerprint, `rules_version` and the components used (`abbr`, `salary_component`, `amount`). No employee name is returned. |
| F1-11 | The tool registry awaits async handlers. |

## 3. Tests (positive, negative, boundary), against a Frappe mock transport built from the verified doctype fields
- **Registries**: valid file loads. Each field rule violated once. Ceilings ≤ 0 or rates outside (0, 1] rejected. Overlap between required and optional components rejected. Missing env var → `HOST_NOT_CONFIGURED`.
- **Fetch**:
  - Correct auth header, filters and paging on the wire.
  - 0 / 1 / 2 slips.
  - HTTP 500 → `HOST_ERROR`.
  - An employee ID containing quotes or `&` travels inside the JSON filter only, never in the URL path.
- **Fingerprint**:
  - The same structure gives the same fingerprint.
  - A renamed field, a type change (amount as a string) or a new earnings key each give a different one.
  - Not approved → `MAPPING_NOT_APPROVED`.
  - Drift → `SCHEMA_DRIFT`, with a quarantine row that holds no amounts.
- **Validation**:
  - Wrong currency.
  - A negative amount.
  - A duplicate `abbr`.
  - Gross mismatch, boundary ±0.01 accepted and ±0.02 rejected.
  - A required component missing versus an optional one missing.
  - Statistical and excluded rows ignored in the gross check.
- **Computation**:
  - Wage below, at and above ₹15,000.
  - Each ceiling flag on and off.
  - Rounding at .5.
  - EPS capped at ₹1,250 when the wage is above ₹15,000 (8.33%).
- **Tool and API**:
  - The LLM cannot pass amounts; trusted `tenant_id` is injected.
  - EMPLOYEE gets `TOOL_NOT_PERMITTED`; non-IN gets `NOT_APPLICABLE`.
  - The approve endpoint needs `MANAGE_SCHEMA` and a fresh token.
  - The quarantine list is tenant-scoped.

## 4. Out of scope
- **F2:** approved, idempotent write-back with rollback.
- **Piece G:** approval workflow and UI.
- **Piece I:** declarative YAML vendor profiles, iCeipts and Zoho.
- **Later:** the Code on Social Security 2020 "50% wage" rule, which needs legal sign-off (NEEDS_HUMAN).

## 5. Amendments after agy review (architect, under delegation)
| # | Amendment |
|---|---|
| F1-6a | The original F1-6 wording ("only fields the calculation reads") contradicted the §3 test list ("a new earnings key gives a different fingerprint"). Resolved in favour of the test list: **every key of every earnings row** is fingerprinted, because earnings rows *are* the wage. Top-level fields the calculation does not read are still ignored. |
| F1-8a | Memo rows (`statistical_component` or `do_not_include_in_total`) are never PF wage. A required component present only as a memo row counts as missing. |
| F1-5a | The fetched document must be the requested one: `docstatus = 1`, `employee` equal to the request, `start_date` inside the month. Otherwise `INCONSISTENT_PAYLOAD`. A malformed listing is `HOST_ERROR`, not `SLIP_NOT_FOUND`. |
| F1-4a | Rules consistency: no duplicate components; `eps_rate ≤ employer_rate`; `eps_wage_ceiling ≤ wage_ceiling`. Host registry: unique tenants; no credentials embedded in `base_url`. |
