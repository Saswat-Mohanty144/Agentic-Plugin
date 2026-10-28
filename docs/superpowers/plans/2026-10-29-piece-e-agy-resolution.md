# Piece E — resolution of agy findings (2026-10-29-piece-e-agy-findings.json)

agy (gemini-3.8-flash-high): verdict FAIL, 11 findings, 35 requirements checked. HEAD unchanged.
Note: an untracked `.claude/sdd-cache/` appeared; investigation showed it holds WebFetch caches of the Frappe doctype pages fetched by Claude at 02:30 (before the agy run) — not an agy rule violation. Added to .gitignore.

| ID | Sev | Status | Resolution |
|---|---|---|---|
| F1 | HIGH | PARTLY DISPUTED / FIXED | Disputed part: consecutive same-role turns are valid — the bundled claude-api reference states "Consecutive same-role messages are allowed - the API combines them into a single turn" (test `test_consecutive_user_turns_are_allowed`). Valid part fixed: a conversation starting with an assistant turn is rejected in the converter, and API history must start with a user turn (422). |
| F2 | HIGH | FIXED | History content min_length 1; empty user text never sent. |
| F3 | MEDIUM | FIXED | Converter raises on a tool result whose id is not a tool_use of the most recent assistant turn (raw_content or tool_calls). |
| F4 | MEDIUM | FIXED | Retry-After HTTP-date parsed; NaN/inf/negative/garbage fall back to backoff; cap 30 s. |
| F5 | MEDIUM | FIXED | `aclose()` on clients and gateway; owned httpx clients closed, injected ones left alone; synthesizer closes the gateway it creates. |
| F6 | MEDIUM | FIXED | History flows through /v1/chat/stream → supervisor.stream_agentic → run_stream. |
| F7 | MEDIUM | FIXED | Stream endpoint resolves LLM configuration before streaming → plain HTTP 503. |
| F8 | LOW | FIXED | Orchestrator handles refusals explicitly: confidence 0.0, no citation scraping (non-vacuous tests). |
| F9 | LOW | FIXED | Lifecycle test counts real `httpx.AsyncClient` constructions and checks closing. |
| F10 | LOW | FIXED | AnthropicClient validates effort at construction. |
| F11 | LOW | FIXED | Single-call stream emits word-level token events; concatenation identical. |

RED: 21 failed + 1 error (4 passed: consecutive users, two valid-history cases, normal-confidence guard). GREEN: 788 passed, ruff clean. No second agy round (DEV_LOOP cap).
