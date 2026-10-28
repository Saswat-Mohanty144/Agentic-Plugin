# TDD log — piece C (data protection on the live LLM path)

Branch feat/piece-c-data-protection (stacked on feat/phase7-stabilise-auth @ d6aeeb4). Plan: tasks C1–C5 below (concise plan under user delegation; each task is test-first).

## C1 — PII detectors, request-scoped session, stream restorer (`security/pii.py`)
- Sample identifiers computed and checksum-verified before use (Aadhaar 234567890124 Verhoeff-valid; Emirates ID 784199012345676 Luhn-valid; IBAN boundaries 15/34 chars mod-97-valid; GB95AAAAAAAAAA mod-97-valid at 14 chars).
- RED: `ModuleNotFoundError: No module named 'hrms_plugin.security.pii'`.
- GREEN: 53 passed on first run → mutation pass (8 mutants): 6 killed; 2 survived = real test gaps:
  - IBAN length floor 15→14 not tested → added `test_mod97_valid_but_14_chars_is_not_iban`.
  - known-value word boundaries not tested (over-redaction of "Ali" inside "Alice") → added `test_known_values_respect_word_boundaries`.
  Both mutants re-applied → killed. Final: 55 passed. Simplified a redundant condition in `redact_text`.

## C2 — LLMGateway single checkpoint (§3.2)
- RED: 8 failed, 1 passed (`test_message_without_pii_is_unchanged` already held — kept as regression guard).
- GREEN: test_gateway_pii.py 9 passed (EchoClient proves the provider saw only tokens; 5-char stream chunks prove split-token restoration; monkeypatched no-op redaction proves fail-closed with provider never called).
- Gateway `mask_pii` parameter removed (decision C4); `pii_gateway` constructor parameter removed. Tool-call arguments are restored inside the gateway (single point), so the orchestrator receives real values — the orchestrator-level test in C3 verifies this end to end.
- Migrated test_agentic_llm.py: dropped `mask_pii=True` (assertion unchanged).
