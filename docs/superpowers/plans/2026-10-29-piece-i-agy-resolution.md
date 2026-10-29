# Piece I — agy findings resolution

agy (gemini-3.8-flash-high) verdict FAIL, 11 findings (its summary line said 8; the file lists F1–F11). All
reproduced; none disputed. Tests: `tests/unit/test_piece_i_agy_fixes.py` (19).

| ID | Sev | Resolution |
|---|---|---|
| F1 | HIGH | ADR context corrected: PF and write-back use the hand-written Frappe client, not the declarative engine |
| F2 | HIGH | Profile validation enforces `headcount: int` and `str` for every other canonical field |
| F3 | MED | Page `size` is `StrictInt` (bool, "50", 50.0 rejected) |
| F4 | MED | Test now asserts the vendor reason (`not a frappe host`); the formerly equivalent mutant is now killed |
| F5 | MED | `bearer_token()` refuses non-iCeipts hosts |
| F6 | MED | iCeipts profile maps `department.name` and `managerId` (both in `JobRequisitionDetails`) |
| F7 | LOW | Canonical `APPROVED` added; iCeipts APPROVED→APPROVED, JOB_CONVERTED→OPEN (architect decision, spec I3 amended) |
| F8 | LOW | Endpoint test with a Frappe host (auth header `token k:s`) |
| F9 | LOW | Upper-case UUIDs accepted and normalised to lower case |
| F10 | LOW | Endpoint test: invalid profile → 500 `PROFILE_INVALID`, no detail leaked |
| F11 | LOW | Query parameter names (paging + `fields`) must be distinct |

Mutation proof (single run): credentials guard → 1 fail; bearer guard → 1; no UUID lower-casing → 1; field types
unchecked → 2; parameter collisions → 4; lax offset size → 3; PROFILE_INVALID handler removed → 1.
Two earlier tests updated for the F7 decision (iCeipts APPROVED now APPROVED). Full suite: ruff clean; pytest 1197 passed.
