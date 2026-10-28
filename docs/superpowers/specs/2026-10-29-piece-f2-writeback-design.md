# Design: Piece F2 — Approved, idempotent write-back with rollback (Frappe)

* **Date**: 2026-10-29 · **Status**: decided by architect (HR domain + AI architecture) under user delegation
* **Answers**: the "Act" layer of the automation loop: an agent may *propose* a change; a different human approves it; the plugin writes it once, and it can be rolled back safely.
* **Rules**: strict TDD; positive, negative and boundary tests; no assumptions.

## 1. Verified facts (2026-10-29)
- Frappe REST: `PUT /api/resource/:doctype/:name` with **only the fields to change**; the response carries the updated document under `data`, including a new `modified`. The docs describe **no** optimistic-concurrency mechanism.
- ERPNext Employee: `notice_number_of_days` (Int) and `final_confirmation_date` (Date) exist. So do the identifier fields `employee_name`, `first_name`, `cell_number`, `personal_email`, `company_email` and `bank_ac_no`.

## 2. Decisions
| # | Decision |
|---|---|
| W1 | Two-phase, human in the loop. **Propose** (`POST /v1/writeback/proposals` {doctype, name, patch}) reads the current document and stores a PENDING proposal: before-values, patch, `base_modified`, `proposed_by`, expiry 24 h. It returns the field-level diff. **Approve** (`POST /v1/writeback/proposals/{id}/approve`) executes it. **Rollback** (`POST /v1/writeback/proposals/{id}/rollback`) restores the before-values. |
| W2 | Permissions: all three need `MUTATE_EMPLOYEE`. Approve and rollback also need a token issued ≤ 300 s ago, and **the approver must not be the proposer** (separation of duties → 403 `SAME_PERSON`). |
| W3 | Writable fields are a **per-tenant allowlist** in the host registry: `writable_fields: {doctype: {field: int / date / string / decimal}}`, default empty (no writes). An allowlist naming an identifier/PII field is rejected at load. Real allowlists: **NEEDS_HUMAN**. |
| W4 | Patch validation: at least one field; every field allowlisted; values type-checked (`int` excludes bool; `date` is a valid `YYYY-MM-DD`; `string` ≤ 140 chars; `decimal` is a finite decimal string). A patch equal to the current values → `NO_CHANGE`. |
| W5 | **Concurrency (our own, since Frappe documents none)**: on approve, re-read the document. If `modified` ≠ `base_modified` → `CONFLICT`; the proposal becomes CONFLICT and nothing is written. On rollback, re-read; if `modified` ≠ the `modified` returned by our write → `ROLLBACK_CONFLICT` and nothing is written. Residual risk (documented): a write landing between our re-read and our PUT. |
| W6 | **Idempotency**: the proposal ID is the idempotency key. Approving an EXECUTED proposal returns the stored result without calling the host; the same applies to rolling back a ROLLED_BACK one. A per-proposal lock makes concurrent double-clicks write at most once. |
| W7 | States: PENDING → EXECUTED → ROLLED_BACK; PENDING → CONFLICT; PENDING → EXPIRED (at 24 h, checked on approve). Any other transition → `INVALID_STATE`. |
| W8 | Every PUT carries the attribution headers from the existing `HostAuditGateway` (invocation ID, approver, reasoning hash). The proposal stores the approval and rollback audit records (who and when). |
| W9 | Proposals are tenant-scoped: another tenant's ID → `NOT_FOUND`. |
| W10 | PII field list extended with Frappe identifier names `cell_number` (phone), `company_email` (email), `bank_ac_no` (bank account), found while verifying W1. |
| W11 | HTTP mapping: NOT_FOUND 404; FIELD_NOT_WRITABLE / INVALID_VALUE / NO_CHANGE 422; CONFLICT / ROLLBACK_CONFLICT / EXPIRED / INVALID_STATE 409; SAME_PERSON 403; HOST_NOT_CONFIGURED 503; HOST_ERROR 502. |
