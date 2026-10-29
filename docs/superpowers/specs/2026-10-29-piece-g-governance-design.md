# Design: Piece G — Governance for LLM data flows and schema mappings

* **Date**: 2026-10-29 · **Status**: decided by architect (HR/legal-minded) under user delegation
* **Answers**: Q3 and Q7 (governance part); Q1 ("LLM suggestions stay pending until a human approves")
* **Rules**: strict TDD; positive, negative and boundary tests; no assumptions.

## Decisions
| # | Decision |
|---|---|
| G1 | **LLM egress audit log.** Every `LLMGateway.chat` / `chat_stream` call records `{tenant_id, at, provider, model, payload_sha256 (redacted payload), redacted_kinds {kind: count} for this call, input_tokens, output_tokens}`. **No content is ever stored.** Namespace `llm_egress`. `GET /v1/governance/llm-egress` (`VIEW_COMPLIANCE`) returns the caller tenant's records, newest first, at most 200. |
| G2 | **Per-tenant LLM policy, fail closed.** The tenant registry entry gains `llm_policy: {allowed_providers: [..], tokenize_compensation: bool}`. A call proceeds only if the deployment's provider is in `allowed_providers`. **No policy → refused.** Refusal → 403 `{"error": "LLM_NOT_ALLOWED_FOR_TENANT"}`, checked before anything is sent. Per-customer content is **NEEDS_HUMAN** (legal / DPA). The test registry declares `fake`. |
| G3 | `tokenize_compensation: true` → compensation fields are tokenised like identifiers. Fields: `salary`, `basic_salary`, `gross_salary`, `ctc`, `annual_ctc`, `gross_pay`, `net_pay`, `basic_wage`, `basic_wage_monthly`, `gross_wage_monthly`, `monthly_gross`, `annual_salary`. Default `false` (decision C2). |
| G4 | **Mapping governance.** Synthesised mappings are saved as **versioned proposals** (`PROPOSED`, `proposed_by`) instead of going live. `POST /v1/schema/mappings/{vendor}/{entity_type}/versions/{version}/approve` needs `MANAGE_SCHEMA`, a token ≤ 300 s old and an approver ≠ proposer; it makes that version `ACTIVE` and the previous active one `SUPERSEDED`. `GET /v1/schema/mappings/{vendor}/{entity_type}` returns the **active** version (404 if none). The list shows every version with its status. |
| G5 | Residual (documented, NEEDS_HUMAN): named-entity recognition for free-text names that appear in no structured record. It needs a heavy NLP dependency (e.g. Presidio + spaCy), which is a product/infrastructure decision. |

## Tests
| Area | Positive | Negative | Boundary |
|---|---|---|---|
| G1 | a record per call, with hash/kinds/tokens; listing scoped to tenant, newest first | the record has no message content and no raw PII; another tenant's records are invisible | 201 records → 200 returned |
| G2 | allowed provider → call proceeds | missing policy → 403; provider not in list → 403; provider never called | empty `allowed_providers` rejected at registry load |
| G3 | toggle on → salary value tokenised before the LLM | toggle off → salary stays (C2) | — |
| G4 | propose → approve → GET returns active; a second approval supersedes | self-approval 403; stale token 401; unknown version 404 | approving an already-active version is idempotent |

## Amendments after agy review (architect, under delegation)
| # | Amendment |
|---|---|
| G2a | **Reverses an original design choice.** Explicit-config gateways without a policy resolver are now refused for every provider except the in-process `fake` (tests). The earlier "only env-built gateways enforce" was fail-open by construction. |
| G1a | Egress rows are written **before** the provider is called (`status` SENT → COMPLETED or FAILED). `redacted_kinds` counts the distinct tokens present in *this call's* payload. Stream token usage is recorded as unknown (`null`), not 0. |
| G3a | Compensation tokenisation covers money-like keys at word level (`net_take_home`, `gratuity_amount`, `employer_epf`; not `payment_days`), nested amounts, and the textual forms of an amount: `45000`, `45000.0`, `45000.00`, `45,000`, and Indian grouping `4,50,000` / `18,00,000`. |
| G4a | The direct active write (`save_mapping`) is private (`_activate`). Version allocation and approval are serialised by a process lock. Keys containing `:` are rejected (`INVALID_KEY`). Multi-replica deployments need a shared store with atomic increments: recorded for production hardening. |
| G1b | The agentic synthesiser re-raises `LlmNotAllowed` and `PiiEgressBlocked` instead of falling back silently. |
