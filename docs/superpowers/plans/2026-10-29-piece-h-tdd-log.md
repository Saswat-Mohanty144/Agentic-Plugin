
After rebasing onto the piece-D fixes (fe2e767), 6 existing tests failed as direct consequences of H1/H6 contract
changes and were updated, preserving their intent: system content is now read from all system messages; message
indexes shift by one (two system messages); the route table and the role matrix include
`POST /v1/payroll/pf/calculate` (VIEW_SALARY: payroll_officer holds, staff lacks).
Full suite: `ruff` clean; `pytest` 1080 passed; `node --test` 79 pass.
