# TDD log — piece F2 (approved idempotent write-back + rollback)

Branch feat/piece-f2-writeback (stacked on F1 @ b7cbd36). Frappe update semantics verified on docs.frappe.io (partial PUT; no documented optimistic concurrency); Employee fields verified in frappe/erpnext develop.

- RED: `ModuleNotFoundError: No module named 'hrms_plugin.writeback'`.
- GREEN: test_writeback.py 34 passed; full suite 943; ruff clean. Stateful Frappe mock (PUT mutates the doc and bumps `modified`).
- Mutation pass (6 mutants): 5 killed first time (separation of duties, approve conflict, rollback conflict, expiry boundary `>`→`>=`, idempotency). Survivor: removing the per-proposal lock — the double-click test never interleaved because the mock answered synchronously. Fixed the test (async handler with a 20 ms delay) → mutant killed.
- Found while verifying: Frappe identifier fields cell_number / company_email / bank_ac_no were missing from the PII field list (affects piece C learning from Frappe records) → added (W10) with tests; such fields can never be configured writable.
