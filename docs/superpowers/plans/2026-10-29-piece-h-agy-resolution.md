# Piece H — agy findings resolution

agy (gemini-3.8-flash-high) verdict FAIL, 8 findings (`2026-10-29-piece-h-agy-findings.json`). All reproduced; 7 fixed
in code test-first, 1 (F7) was a spec wording error and the spec was corrected. Tests: `tests/unit/test_piece_h_agy_fixes.py`.

| ID | Sev | Resolution |
|---|---|---|
| F1 | HIGH | Stream synthesis sends the same tools with `tool_choice: none` (Anthropic) so the cached prefix matches; per the official invalidation hierarchy a `tool_choice` change keeps the tools+system cache |
| F2 | HIGH | Live evals require `HRMS_EVAL_TENANT` (a registered tenant with an llm_policy); `run_case`/`run_all` take the tenant |
| F3 | HIGH | Gateway stream wrapped in try/finally: rows end COMPLETED / ABORTED / FAILED; Anthropic emits `usage` first and it is recorded immediately, so a consumer that disconnects is still charged |
| F4 | MED | Per-tenant/UTC-day running counter (`llm_egress_daily`), updated by each row's token delta under a lock; `tokens_today` is one read |
| F5 | MED | End-to-end HTTP tests with a real budget, egress log and gateway: 429 on `/v1/chat`; in-band `LLM_BUDGET_EXHAUSTED` when the budget runs out between ReAct turns of `/v1/chat/stream` (removing the handler now fails a test) |
| F6 | MED | `chat_stream` is text-only by contract: tools are offered only with `tool_choice: none`; the budgeted fallback offers none, so no tool call can be dropped |
| F7 | LOW | Spec corrected: other providers receive the system messages separately (code was right) |
| F8 | LOW | Oversized output keeps a short string error code as `tool_error` |

Mutation proof (single run): no in-band budget event → 1 fail; synthesis without tools → 1; no `tool_choice: none` → 1;
tools offered to any client → 2; no finally accounting → 3; usage not first → 1; counter double-counts → 2;
eval tenant check removed → 1; error code dropped → 1.
Known bound (documented, not hidden): a request cancelled while awaiting the provider is marked FAILED with no usage;
the provider may still bill it. Concurrent in-flight calls can overshoot the budget by at most one call each.
Full suite: ruff clean; pytest 1099 passed.
