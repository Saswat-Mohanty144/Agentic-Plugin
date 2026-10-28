# Piece F1 — resolution of agy findings (2026-10-29-piece-f1-agy-findings.json)

agy (gemini-3.8-flash-high, briefed as payroll + API-security verifier): FAIL, 13 findings, 44 requirements checked; changed no files except its report. All reproduced or visible in code; none disputed.

| ID | Sev | Status | Resolution |
|---|---|---|---|
| F1 | HIGH | FIXED (F1-8a) | Memo rows never count as PF wage; required component only as memo row → PF_COMPONENT_MISSING. |
| F2 | MEDIUM | FIXED | Duplicate components rejected at load. |
| F3 | MEDIUM | FIXED | eps_rate ≤ employer_rate; eps_wage_ceiling ≤ wage_ceiling (no negative employer EPF). |
| F4 | MEDIUM | FIXED | Non-dict JSON → HOST_ERROR (TypeError caught). |
| F5 | LOW | FIXED | Non-list listing → HOST_ERROR. |
| F6 | MEDIUM | FIXED (F1-6a) | All earnings-row keys fingerprinted; my earlier test asserting the opposite was rewritten. |
| F7 | MEDIUM | FIXED (F1-5a) | Document must be submitted, same employee, start date in month. |
| F8 | LOW | FIXED | Userinfo in base_url rejected. |
| F9 | LOW | FIXED | Duplicate tenants in HRMS_HOSTS rejected. |
| F10 | LOW | FIXED | Vacuous self-assignment removed. |
| F11 | LOW | FIXED | Gross-check test asserts pf_wage 14000. |
| F12 | LOW | FIXED | Missing (employee uncapped, employer capped) combination: 3000 / 1800 / 1250 / 550. |
| F13 | LOW | FIXED | Quarantine listing test seeds rows for two tenants and asserts tenant scoping. |

Found while fixing: two service tests used employee "E" against a slip for HR-EMP-00001. After F7 one failed, and the currency test kept passing for the wrong reason (employee mismatch). Both corrected; a precise currency test added.
RED: 18 failed → GREEN: 900 passed, ruff clean. No second agy round (DEV_LOOP cap).
