# TDD log — piece E (LLM layer)

Branch feat/piece-e-llm-layer (stacked on piece C @ 3d966bf). anthropic 1.17.0 verified: beta.messages.create supports betas/fallbacks/output_config; non-beta create has no temperature param. Mock transport (httpx2.MockTransport) verified end to end before writing tests.

## E-a — providers: Anthropic SDK client, refusal, fail-loud config, retries, Gemini header (E1, E2-converter, E3, E6, E7)
- RED: `ImportError: cannot import name 'LLMNotConfigured'`.
- GREEN: test_llm_providers.py 23 passed — wire-level checks: model claude-opus-5-5, max_tokens 16000, output_config.effort, fallbacks "default" + beta header, no temperature; thinking block replayed byte-identical; 2 tool_results grouped in one user message; refusal → safe text, no tools, no raw replay; nothing/unknown provider → LLMNotConfigured; retries 429/503/529 with Retry-After honoured and capped at 30 s; 400 not retried; one shared httpx client; Gemini key in x-goog-api-key header, not URL.
- Full suite 740 passed.

## E-b — orchestrator: one assistant message per turn, raw_content carried, single-call stream (E2, E3, E4)
- RED: 6 failed (refusal tests already passed — a refusal has no tool calls so the loop returned its text; kept as guards).
- Updated `test_stream_without_tools_reports_general_hr`: it scripted two responses to document the double call; now one.
- GREEN: orchestrator suites 96 passed.

## E-c — synthesizer applies LLM matches (E5)
- RED: 4 failed (FieldMap TypeError swallowed → LLM match lost; no log; config error swallowed), 1 passed (unknown host field ignored — guard).
- GREEN: 5 passed; full suite 752. `LLMNotConfigured` now propagates (route is LLM-backed); other LLM failures logged as LLM_MAPPING_FAILED with the rule mapping returned.

## E-d — 503 on unconfigured LLM, strict history (E6 HTTP, E8)
- RED: 8 failed, 3 passed (non-agentic route already unaffected; 2 valid-history cases already accepted — guards).
- GREEN: test_chat_history.py 11 passed. History items: role user|assistant only, content str ≤ 32,000, ≤ 50 items, no extra keys. LLMNotConfigured → 503 {"error":"LLM_NOT_CONFIGURED"}; on streams → in-band SSE error event (HTTP 200 already committed, as C13).
