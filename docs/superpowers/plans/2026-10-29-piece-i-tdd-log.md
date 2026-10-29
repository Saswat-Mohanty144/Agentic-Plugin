# Piece I — TDD log

Red: `tests/unit/test_declarative_connector.py` written first; collection failed with
`ModuleNotFoundError: No module named 'hrms_plugin.integrations.declarative'` (file-level red). Per-test proof by
mutation below. Green: 75 → 76 tests (one defence-in-depth test added after a surviving mutant).

Design change found by checking the source (no assumption): the iCeipts swagger does not document the JWT's claims,
so the planned `jwt_subject` code hook was dropped; `{service_user_id}` comes from host config (the documented
`user.id` of `POST /employee/login`). Result: second vendor onboarded with **zero** code hooks (gate allows ≤ 1).

Mutation proof (single run, each reverted):
| Mutant | Failing |
|---|---|
| status passes through unmapped | 2 |
| headcount 0 accepted | 1 |
| bool accepted as int | 1 |
| lenient totalPages | 2 |
| offset paging disabled | 3 |
| page paging disabled | 1 |
| any path parameter allowed | 2 |
| path parameter not encoded | 0 → added `test_path_parameter_is_encoded_even_if_validation_were_bypassed` → 1 |
| vendor/profile mismatch allowed | 1 |
| record id unchecked | 1 |
| iCeipts host accepts Frappe/write-back fields | 2 |
| `credentials()` vendor guard removed | 0 — equivalent mutant: an iCeipts host has no key env names, so it raises anyway |
| payroll vendor guard removed | 1 |
| write-back vendor guard removed | 1 |
