# TDD log — piece C (data protection on the live LLM path)

Branch feat/piece-c-data-protection (stacked on feat/phase7-stabilise-auth @ d6aeeb4). Plan: tasks C1–C5 below (concise plan under user delegation; each task is test-first).

## C1 — PII detectors, request-scoped session, stream restorer (`security/pii.py`)
- Sample identifiers computed and checksum-verified before use (Aadhaar 234567890124 Verhoeff-valid; Emirates ID 784199012345676 Luhn-valid; IBAN boundaries 15/34 chars mod-97-valid; GB95AAAAAAAAAA mod-97-valid at 14 chars).
- RED: `ModuleNotFoundError: No module named 'hrms_plugin.security.pii'`.
- GREEN: 53 passed on first run → mutation pass (8 mutants): 6 killed; 2 survived = real test gaps:
  - IBAN length floor 15→14 not tested → added `test_mod97_valid_but_14_chars_is_not_iban`.
  - known-value word boundaries not tested (over-redaction of "Ali" inside "Alice") → added `test_known_values_respect_word_boundaries`.
  Both mutants re-applied → killed. Final: 55 passed. Simplified a redundant condition in `redact_text`.
