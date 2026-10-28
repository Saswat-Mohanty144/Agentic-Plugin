# Piece F2 — resolution of agy findings (2026-10-29-piece-f2-agy-findings.json)

agy (gemini-3.8-flash-high, records-integrity + security brief): FAIL, 7 findings; changed no files except its report. All valid; none disputed.

| ID | Sev | Status | Resolution |
|---|---|---|---|
| F1 | HIGH | FIXED | Rollback PUT carries fresh attribution headers (new invocation id); proposal stores `rollback_audit`. |
| F2 | HIGH | FIXED | Decimal fields compare numerically against host numbers (`"1500000.50"` == 1500000.5 → NO_CHANGE). |
| F3 | MEDIUM | FIXED | `middle_name` added as NAME; cannot be configured writable. |
| F4 | MEDIUM | FIXED | Missing `modified` after a write → re-read the document; if still unknown the proposal stays EXECUTED (never PENDING, which would allow a second write) and rollback returns 409 ROLLBACK_UNAVAILABLE. |
| F5 | LOW | FIXED | Host error detail preserved (`HTTP 500`), never response bodies. |
| F6 | MEDIUM | FIXED | Rollback test asserts headers and audit record. |
| F7 | LOW | FIXED | Decimal tests: NO_CHANGE, invalid forms (float, NaN, Infinity, bool, garbage), exact write and restore. |

RED: 5 failed → GREEN: 954 passed, ruff clean. No second agy round (DEV_LOOP cap).
