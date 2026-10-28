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

## C3 — orchestrator: context keys only, generic trusted injection, learn tool outputs (§3.3)
- RED: 6 failed + 1 error, 1 passed (`request jurisdiction beats context` already held from A5 → regression guard).
- First GREEN attempt exposed a PRE-EXISTING bug (baseline 3028359, orchestrator.py:188,249): `json.dumps(tool_output)` raised `TypeError: Object of type Decimal is not JSON serializable` → every agentic run using `evaluate_onboarding_offer` crashed. Fixed with `default=str`; dedicated regression test added.
- TRUSTED_CONTEXT_PARAMS ("employees" only) replaced by generic injection: any context key matching a declared tool parameter (server-owned jurisdiction/tenant_id excluded from context and always set by the server).
- GREEN: test_orchestrator_pii.py 8 passed (adaptive TokenEchoingClient proves an LLM-written token reaches the tool as the real name); full suite 686 passed.

## C4/C5 — server wiring, 422 on egress block, legacy purge, removal of old modules
- C9 (HR-domain refinement): RED 2 failed (grouped bad-checksum Aadhaar not detected) → GREEN; regression found on the way: card `4111 1111 1111 1111` matched as grouped Aadhaar → added boundary lookarounds + test; 61 passed.
- Server: RED 4 failed, 2 passed (chat path already protected by C2 gateway — kept as guards) → GREEN 6 passed: LLM sees no Aadhaar/name on chat and stream; stream tokens restored across 4-char chunks; `mask_pii:false` still protected; egress block → 422 `{"error":"PII_EGRESS_BLOCKED","kinds":["AADHAAR"]}` with no value in the body; legacy `pii_vault*` rows purged at import (subprocess test on a pre-seeded DB), other namespaces kept; old modules gone.
- Process slip (recorded): a `git rm` ran although the preceding edit script aborted (not chained with &&). The deletions were the intended ones; the edit was re-applied with a corrected anchor before any test run or commit.
- Removed tests of removed behaviour: test_security.py (2 PiiMaskingGateway tests), test_storage.py (vault restart persistence — removed by design C5). Covered more strictly by test_pii.py / test_gateway_pii.py.
- Full suite: 695 passed, ruff clean.
