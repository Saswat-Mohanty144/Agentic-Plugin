# README section 20 gaps: plan and decisions (2026-10-30)

Branch `agentic-ai`. Each task runs in its own worktree (`/Users/satosh/Agentic-Plugin-gap-<X>`, branch
`gap/<x>`), test-first: the new tests must fail before the change (red output saved to
`docs/superpowers/plans/2026-10-30-gap-<x>-red.txt`) and pass after. Positive, negative and boundary cases for
every behaviour. Decisions below were taken by the architect under the user's standing delegation.

Interpreter: `/Users/satosh/Agentic-Plugin-main/.venv/bin/python`.

## Decisions

| ID | Gap | Decision |
|---|---|---|
| D1 | `agentic:false` path invents inputs (CTC 1,200,000; basic 15,000; tenure 3.5; any other default) | No defaults. A specialist that lacks a required input answers with `structured_data.error = "MISSING_INPUT"` and `missing: [...]`, confidence 0, and no figures. Supplied inputs keep working. |
| D2 | Streaming runs one tool round | `run_stream` uses the same loop as `run`: up to `max_iterations` tool rounds (events per tool as today), then the final answer streams with tools forbidden. Hitting the limit emits `{"event":"partial","data":true}` before the tokens. `done` carries `finish_reason` and `truncated`. The no-tool first answer is still streamed without a second call. |
| D3 | Field classification stored, never read | The orchestrator resolves the tenant's ACTIVE mapping for the context's entity (`context["entity"]` upper-cased with spaces → `_`, vendor from `HRMS_HOSTS` for the tenant). Context keys classified `llm_visible: true` (only PUBLIC/INTERNAL can be) have their values shown to the model in addition to `entity`, `record_id`, `route`. Values of keys classified PII are learned by the PII session (redacted everywhere); COMPENSATION values are learned when the tenant sets `tokenize_compensation`. Without a mapping or classification, behaviour is unchanged. |
| D4 | Synthesize routes store vendor `host`/`custom`; overlay rejects synthesizer transforms | Synthesize requests take `vendor` (pattern `^[a-z0-9_-]{1,32}$`, default `host`). Declarative `FieldSpec` gains optional `transform` (`strip`/`lower`/`upper`, applied to strings) and `required` (missing or empty value → row rejected `MISSING_VALUE`). The overlay passes these through instead of refusing them. Defaults remain refused. |
| D5 | Shadow prober and drift interceptor not called | Synthesize requests accept optional `sample_records` (≤ 50). When given, `ShadowProber.probe` runs on the proposed mapping and the result (`is_valid`, `quality_score`, issues) is returned and stored on the version. Approving a version whose stored probe is invalid → `409 PROBE_FAILED`. Introspect with `sample_records` runs `SchemaDriftInterceptor.inspect_response` against the tenant's ACTIVE mapping for that entity, when one exists, and returns `drift: [events]`. |
| D6 | Rollback, fingerprint approval and shape approval are one-person | Two-step, different people. First call records `requested_by` and returns `202` with status `PENDING_CONFIRMATION` (rollback: `ROLLBACK_PENDING`). The same person again → `403 SAME_PERSON`. A different authorised person → executes. A pending fingerprint/shape request is tied to the exact fingerprint; a different fingerprint replaces it. |
| D7 | Audit headers hashed, not signed | Ed25519 signature. Private key PEM path in `HRMS_AUDIT_SIGNING_KEY_FILE`, key id in `HRMS_AUDIT_KEY_ID`. Headers `X-Audit-Signature` (base64url) and `X-Audit-Key-Id`; the signed string is the other audit headers as `name:value` lines sorted by name. `verify_audit_headers(headers, public_key)` provided. No key → host writes and remediation refuse with `503 AUDIT_SIGNING_NOT_CONFIGURED` (fail closed). |
| D8 | MCP adapter has no transport | `POST /v1/mcp`: MCP Streamable HTTP, JSON responses (no SSE). JSON-RPC 2.0 methods `initialize`, `notifications/initialized` (202, no body), `ping`, `tools/list`, `tools/call`. Bearer auth and `USE_COPILOT`, rate-limited, `Origin` must be an allowed origin when present. Jurisdiction from header `X-HRMS-Jurisdiction` (required, IN/AE/SA/US). `GET /v1/mcp` → 405. |
| D9 | Widget does not stream or show withheld audio | Widget uses `/v1/chat/stream` (fetch body reader, `data:` frames), renders tokens as they arrive, shows tool steps, and falls back to `/v1/chat` when streaming is not supported. When `audio_withheld` is set it shows a short notice. |
| D10 | No persistent volume; per-process limits | Compose: named volume for `/var/lib/hrms`. k8s: PVC, `replicas: 1`, strategy `Recreate` (SQLite is single-writer), pod `securityContext` (non-root 10001, no privilege escalation, read-only root, `/tmp` emptyDir), secrets via `envFrom` secretRef. Unused env vars removed. Horizontal scaling needs a shared store: hosting topology is an owner decision. |
| D11 | CI runs on `main` only | CI triggers on every branch push and on pull requests. |
| D12 | Labour-law corpus has 10 citations | PARKED, NEEDS_HUMAN (legal reviewer): new statutory citations must come from a reviewed source; inventing them is not acceptable. |

## Tasks

| Task | Decisions | Main files |
|---|---|---|
| A | D1 | `agents/supervisor.py` |
| B | D2 | `agents/orchestrator.py`, `server/app.py` (stream route) |
| C | D3 | `agents/orchestrator.py`, `storage/mapping_store.py` |
| D | D4, D5 | `server/app.py` (schema routes), `schema/`, `integrations/declarative.py`, `integrations/mapping_overlay.py`, `storage/mapping_store.py` |
| E | D6, D7 | `writeback/service.py`, `payroll/service.py`, `security/audit.py`, `server/app.py` |
| F | D8 | new `server/mcp_http.py`, `server/app.py` (include router) |
| G | D9 | `sdk/copilot.js`, `sdk/test/` |
| H | D10, D11 | `deploy/`, `.github/workflows/ci.yml` |

Acceptance for every task: its new tests and the previously passing tests in the touched areas pass; `ruff check
hrms_plugin tests` clean. After merging: full suite, one mutation proof per batch, one review round.

## Review round (2026-10-30) findings and decisions

| ID | Finding | Decision |
|---|---|---|
| R1 | Mappings default to vendor `host`, chat resolves the host's real vendor, so classification is silently off | `vendor` on synthesize/introspect defaults to the tenant's `HRMS_HOSTS` vendor when configured, else `host` |
| R2 | `/v1/mcp` returns host data to an external model client without the tenant's data policy | New tenant policy flag `mcp_enabled` (default false). MCP requires it and `max_data_tier >= 2`, else `403 MCP_NOT_ALLOWED_FOR_TENANT`; every `tools/call` writes an egress row (provider `mcp-client`). Residency cannot be verified for an external client; documented |
| R3 | Pending two-person requests never expire | 24 h expiry (same as proposals); an expired request counts as absent and the next call starts a new one |
| R4 | Deploy files never mount the signing key, hosts or rules files | Compose and k8s mount them read-only from a secret/config |
| R5 | Stream `result` misses citations written in the answer; refusal not handled | Same `_collect_from_text` and refusal handling as `run()` |
| R6 | MCP origin check uses every tenant's origins | Must be one of the caller tenant's `allowed_origins` |
| R7 | Introspect 500 on `:` in an entity name | `400 INVALID_KEY` |
| R8 | Drift flags parent keys of nested source paths | Compare against the top-level segment of each source path |
| R9 | Currency defaults to AED for SA/US | Derived only for IN (INR) and AE (AED); otherwise required (`MISSING_INPUT`) |
