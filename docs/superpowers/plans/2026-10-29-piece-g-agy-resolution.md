# Piece G — resolution of agy findings (2026-10-29-piece-g-agy-findings.json)

agy (gemini-3.8-flash-high, data-protection + security brief): FAIL, 12 findings; changed no files except its report. All accepted (F8 reverses my own documented design). None disputed.

| ID | Sev | Status | Resolution |
|---|---|---|---|
| F1 | HIGH | FIXED (G1b) | Agentic synthesis surfaces LlmNotAllowed/PiiEgressBlocked. |
| F2 | HIGH | FIXED (G1a) | Per-call distinct-token counts, independent of session history. |
| F3 | HIGH | FIXED (G3a) | Amount variants incl. Indian lakh grouping. |
| F4 | MEDIUM | FIXED (G3a) | Nested compensation values learned. |
| F5 | MEDIUM | FIXED (G3a) | Word-level money-key detection; non-money counters untouched. |
| F6 | HIGH | FIXED (G4a) | INVALID_KEY for ':' in key parts. |
| F7 | MEDIUM | FIXED (G4a) | RLock around propose/approve (8-thread test → versions 1..8). |
| F8 | HIGH | FIXED (G2a) | Explicit real-provider gateway without resolver → LlmNotAllowed. |
| F9 | MEDIUM | FIXED (G1a) | Record before sending; FAILED status on error. |
| F10 | MEDIUM | FIXED | Tests added for agentic synthesis refusal, multi-turn counts, number formats. |
| F11 | LOW | FIXED (G4a) | save_mapping no longer public; test_storage migrated to propose → approve. |
| F12 | LOW | FIXED (G1a) | Stream tokens null (unknown). |

Process slip (recorded): while restructuring LLMGateway I replaced a span that also contained _redact_obj/_protect/_resolve_config_from_env/_create_client (114 tests failed). Restored those methods verbatim from HEAD and the import the auto-fixer had removed; no commit contained the broken state.
Also updated: test_piece_e_agy_fixes gateway-close test now uses tenant acme (DEFAULT is refused under G2, as intended).
RED: 21 failed → GREEN: 1009 passed, ruff clean. No second agy round (DEV_LOOP cap).
