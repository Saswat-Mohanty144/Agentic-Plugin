# TDD log — piece E (LLM layer)

Branch feat/piece-e-llm-layer (stacked on piece C @ 3d966bf). anthropic 1.17.0 verified: beta.messages.create supports betas/fallbacks/output_config; non-beta create has no temperature param. Mock transport (httpx2.MockTransport) verified end to end before writing tests.

## E-a — providers: Anthropic SDK client, refusal, fail-loud config, retries, Gemini header (E1, E2-converter, E3, E6, E7)
- RED: `ImportError: cannot import name 'LLMNotConfigured'`.
- GREEN: test_llm_providers.py 23 passed — wire-level checks: model claude-opus-5-5, max_tokens 16000, output_config.effort, fallbacks "default" + beta header, no temperature; thinking block replayed byte-identical; 2 tool_results grouped in one user message; refusal → safe text, no tools, no raw replay; nothing/unknown provider → LLMNotConfigured; retries 429/503/529 with Retry-After honoured and capped at 30 s; 400 not retried; one shared httpx client; Gemini key in x-goog-api-key header, not URL.
- Full suite 740 passed.
