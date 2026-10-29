# Piece H — cost controls and evaluations (Q4)

Status: decided by architect under user delegation (2026-10-29). Base: piece D branch.
Sources: claude-api skill `shared/prompt-caching.md` (prefix invariant, render order tools→system→messages,
max 4 breakpoints, Opus 5.5 minimum 512 tokens, top-level automatic caching, usage fields).

## Adversarial audit of current token spend
| # | Finding (code) | Cost / risk |
|---|---|---|
| C1 | `_system_message` interpolates jurisdiction, tenant id and context into ONE system string (orchestrator.py) | No reusable prefix across tenants/requests; up to 5 ReAct turns each re-send tools+system uncached |
| C2 | No `cache_control` anywhere in `AnthropicClient.chat` | Every turn pays full input price for the whole history |
| C3 | `definitions_for` returns registration order; same set per role but order not guaranteed stable by contract | Silent cache invalidator (tools render at position 0) |
| C4 | `usage` keeps only input/output tokens | Cache reads/writes invisible; cannot prove caching works |
| C5 | `chat_stream` finishes egress without tokens ("stream token usage is not reported") | Any budget would be bypassed via `/v1/chat/stream` |
| C6 | Tool results sent as `json.dumps(output, default=str)` (spaced separators, unbounded size) | Wasted tokens; one large tool output can blow the context |
| C7 | PF calculation reachable only through the LLM tool | Deterministic arithmetic paying LLM cost and egress |
| C8 | No per-tenant cap | One tenant (or a runaway loop) spends unbounded tokens |

## Decisions
- H1 Prompt caching (Anthropic): orchestrator emits two system messages — the frozen `SUPER_AGENT_SYSTEM_PROMPT`
  first, the per-request block (jurisdiction, tenant, context) second. `AnthropicClient` renders system as text
  blocks; the FIRST block gets `cache_control: {"type": "ephemeral"}` (caches tools + frozen system), and the request
  carries top-level `cache_control: {"type": "ephemeral"}` (automatic caching for the growing ReAct tail). 5-minute
  TTL: ReAct turns are seconds apart. Other providers receive the two system messages separately (OpenAI-compatible: two `system` messages; Gemini: two `system_instruction` parts — previously Gemini kept only the last one, a bug fixed here). (agy H-F7: wording corrected.)
- H1a (agy H-F1/F6) `chat_stream` is the final-text call only: it sends the same tools with `tool_choice: none` where the provider supports it (Anthropic; a `tool_choice` change keeps the tools+system cache), otherwise no tools; it can never return a tool call.
- H2 Tools are sorted by name in `definitions_for` (deterministic prefix).
- H3 Usage: `cache_creation_input_tokens` and `cache_read_input_tokens` are recorded alongside input/output in
  `LLMResponse.usage` and the egress log. The Anthropic stream emits a `usage` event FIRST (not forwarded to the
  client); the gateway records it at once and marks the row COMPLETED / ABORTED (consumer stopped) / FAILED (agy H-F3).
- H4 Per-tenant budget: `LlmPolicy.daily_token_budget: Optional[int] (>=1)`. Absent → no cap (value is owner input:
  NEEDS_HUMAN). Day = UTC calendar day. Counted = input + output + cache_creation + cache_read tokens of the
  tenant's egress rows that day, kept as a running per-tenant/day counter updated with each row's delta (agy H-F4). Checked before every send (chat and stream);
  exceeded → `LlmBudgetExceeded` → HTTP 429 `{"error": "LLM_BUDGET_EXHAUSTED"}` (in-band SSE error event if it trips between ReAct turns of a stream). A tenant with a budget never
  streams through a client whose stream reports no usage: the gateway uses `chat` and re-emits the text.
  Known bound: concurrent in-flight calls can overshoot by at most (concurrency × one call); documented, not hidden.
- H5 Tool results to the LLM: compact JSON (`separators=(",", ":")`, `default=str`); above 20 000 chars the LLM
  receives `{"error": "TOOL_OUTPUT_TOO_LARGE", "chars": n}` instead; the UI still gets full `structured_data`.
- H6 No-LLM PF path: `POST /v1/payroll/pf/calculate {employee_id, period}` (permission VIEW_SALARY, tenant and
  jurisdiction IN from the token/server) calls `PayrollService.calculate_pf` directly; zero LLM egress.
- H7 Rejected: per-intent tool subsets. They fragment the cache (a different tool list = a different prefix) and a
  mis-classified intent hides the right tool. RBAC subsets remain (one cache entry per role).
- H8 Evals: `hrms_plugin/evals/` with a versioned case file (prompt → expected first tool and forbidden tools) and a
  scorer. Offline mode (CI) runs the cases through the scripted LLM to prove the harness and scorer; it also asserts
  the prefix-stability property (tools + frozen system byte-identical across tenants/jurisdictions) and a ceiling on
  rendered prefix size per role. Live mode (`--live`) spends real money and is NEEDS_HUMAN (approval + budget).

## Out of scope / NEEDS_HUMAN
- Budget values per tenant; live eval run approval and budget; 1-hour TTL decision once real traffic gaps are known.
