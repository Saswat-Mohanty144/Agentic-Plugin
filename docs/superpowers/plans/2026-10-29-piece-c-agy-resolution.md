# Piece C — resolution of agy findings (2026-10-29-piece-c-agy-findings.json)

agy (gemini-3.8-flash-high): verdict FAIL, 12 findings, 35 requirements checked; changed no files except its report (HEAD unchanged). All 12 reproduced by Claude; none disputed.

| ID | Sev | Status | Evidence |
|---|---|---|---|
| F1 | HIGH | FIXED | stream SSE `json.dumps(chunk, default=str)`; test `test_stream_survives_decimal_tool_output` |
| F2 | HIGH | FIXED (C13) | in-band SSE error event; test `test_stream_egress_block_emits_error_event` (provider never called, no value in body) |
| F3 | HIGH | FIXED | case-insensitive learned identifiers; restore keeps original casing; tests in test_pii_hardening.py |
| F4 | HIGH | FIXED | lowercase IBAN detected (mod-97 on uppercased value) |
| F5 | HIGH | FIXED | spaced/hyphenated Indian mobiles; negatives kept (first digit 5, 11 digits) |
| F6 | MEDIUM | FIXED | mixed separators accepted when Verhoeff-valid; mixed + bad checksum still rejected |
| F7 | MEDIUM | FIXED | `normalise()` maps Devanagari/fullwidth digits |
| F8 | MEDIUM | FIXED | `normalise()` strips Cf characters (zero-width etc.), including inside learned names |
| F9 | MEDIUM | FIXED | `ChatMessage.to_dict` uses `default=str` |
| F10 | MEDIUM | FIXED (C12; reverses an original spec boundary) | lowercase/mixed-case PAN detected; invalid holder type still rejected in any case |
| F11 | LOW | FIXED | gateway test now records what the provider received and asserts no raw email/phone |
| F12 | LOW | FIXED | leading-digit-0 Aadhaar negative added |

Mutation proof for the hardening: removing normalisation → 1 failed; dropping IGNORECASE on learned names → 2 failed; dropping IGNORECASE on PAN → 2 failed.
Suite after fixes: 717 passed, ruff clean. Per DEV_LOOP cap there is no second agy round for piece C.
Residual (documented, C8): free-text names that appear in no structured record are not detectable without NER → piece G.
