# Implementation status audit — 2026-10-29 (branch feat/e2e-verification @ 750967d)

Method: every HTTP route in `hrms_plugin/server/app.py` and every agent tool in `hrms_plugin/agents/tools.py` read for
what it actually calls, plus a scan of production code for canned data, silent fallbacks and hard-coded defaults.

## Implemented (real logic, tested)

| Area | Routes / tools |
|---|---|
| Auth, tenants, RBAC | every route (JWT + JWKS, role map, permission per route, token age for remediation) |
| Chat / ReAct agent | `/v1/chat`, `/v1/chat/stream` (LLM gateway: tenant policy, budget, PII redaction, egress log) |
| Schema mapping | `/v1/schema/introspect`, `/synthesize`, `/synthesize/agentic`, `/mappings*`, version approve, fingerprints |
| Write-back | `/v1/writeback/proposals`, approve (four-eyes), rollback — proven live against Frappe v15 |
| Payroll PF | `/v1/payroll/pf/calculate`, tool `calculate_pf_contribution` — exact values proven live |
| Host requisitions | `/v1/hosts/requisitions` (declarative YAML profiles) — 105 records paged live |
| Governance | `/v1/governance/llm-egress` |
| Voice | `/v1/voice/*` — 503 without key, tenant opt-in, egress log, PII gate (VOICE-1/2) |
| Remediation | `/v1/remediate/execute` — attribution headers only, by design (writes go through write-back) |
| Deterministic tools | salary (IN, AE), EOSB/gratuity (IN, AE), leave, travel anomaly, ATS score, JD bias, offer, docs, F&F timeline, final settlement, expense audit, archetypes (template) |

## Partial or misleading (found in this audit)

| ID | Where | What is wrong | Risk |
|---|---|---|---|
| P1 | `GET /v1/compliance/diagnose` (+ `/api/...`) | Audits two hard-coded fictional employees, not the tenant's data; unknown jurisdiction silently becomes UAE; compliant score is 92, not 100 | HIGH — HR sees invented violations as their own |
| P2 | `GET /v1/compliance/dashboard/live` (+ `/api/...`) | Fully hard-coded: every statutory item always "compliant" (MOHRE, WPS, EPFO, ...) | HIGH — false assurance of compliance |
| P3 | tools `calculate_salary_structure`, `calculate_eosb_gratuity` | Accept SA and US but compute India law for them (PF/ESI, Gratuity Act) | HIGH — wrong statutory numbers |
| P4 | tools `compute_final_settlement`, `verify_onboarding_documents`, `evaluate_onboarding_offer` | Request jurisdiction SA/US silently computed as UAE (settlement, docs) or India (offer) | HIGH — wrong law applied |
| P5 | tool dispatcher `AgenticToolRegistry.execute` | Never validates LLM arguments against the declared schema (required, type, enum) | MEDIUM — enables P6 |
| P6 | tool `evaluate_leave_request` | Missing balance is assumed to be 15 days | MEDIUM — leave decided on an invented balance |
| P7 | tool `score_candidate_ats` | Missing skills list becomes a hard-coded Python/FastAPI/PostgreSQL/Docker list | MEDIUM — candidates scored against invented criteria |
| P8 | `/v1/requisitions/generate-archetypes` and tool | Template, not generation: fixed salaries (120k–160k, no currency), fixed `validTill` 2026-12-31, fixed benefits | MEDIUM — labelled as AI output |
| P9 | `POST /api/compliance/agent/chat` (legacy) | Jurisdiction guessed from keywords ("uae", "aed", "eosb" → AE, else IN); body is an untyped dict | LOW–MEDIUM |
| P10b | `hrms_plugin/connectors/` (incl. `iceipts.py`, `bamboohr.py`) | Not imported by any production code; only tests use it. Kept by owner decision | LOW — coverage there tests unused code |
| P10 | tool `audit_statutory_compliance` | Called without employees it audited a fictional "Tariq Al-Mansoor" | HIGH |
| P11 | `hrms_plugin/server/mcp_server.py` (`HrmsMcpServer`) | Parallel tool path with the old invented defaults (balance 10, 3 years' experience, 40% basic), `tenant_id` taken from arguments, no auth, no schema validation. Not served by any transport today | MEDIUM (dormant) |

## Resolution of P1-P10 (commit after 750967d)

- P1/P2: 503 `COMPLIANCE_SOURCE_NOT_CONNECTED` unless `HRMS_DEMO_ENDPOINTS=true`; demo answers carry `data_source: DEMO_SAMPLE`; diagnose rejects jurisdictions other than AE/IN with 422 `JURISDICTION_NOT_SUPPORTED`.
- P3/P4/P10: statutory tools declare AE/IN only; the dispatcher answers `JURISDICTION_NOT_SUPPORTED` (with `supported`) before any handler runs; the regulation search keeps IN/AE/SA/US. The audit tool requires at least one employee.
- P5: `AgenticToolRegistry.execute` checks required, type (bool is never a number), enum, minItems and unknown arguments; failures return `INVALID_ARGUMENTS` with `problems` so the model can correct itself.
- P6/P7: leave balance and ATS criteria (skills, both experience values) are required; no defaults.
- P8: archetypes keep their structure but pay, salary range, validity and benefits are null/empty; responses carry `source: TEMPLATE`, `requires_review: true`.
- P9: legacy chat takes a typed body; `jurisdiction` is required and validated; keywords are never used.
- Proof: tests/unit/test_partial_features_fixed.py, 96 tests, 75 red before the change; validation-off mutant fails 15.
- P11 is scheduled with the Q-gap work below.

## Q1-Q7 audit (read-only Claude review, 2026-10-29)

The safety mechanisms promised in PHASE_WISE_PLAN Q1-Q7 exist only for the Frappe PF path; the approved-mapping
workflow is not read by any calculation. Code gaps, ranked: (1) No-LLM-Numbers gate violated by salary, EOSB, final
settlement and offer tools; (2) approved mapping/FieldMap not on any calculation's read path; (3) no canonical salary
model or cross-checks (basic <= gross <= ctc/12, currency vs jurisdiction) and no MISSING_INPUT rule; (4) provenance not
persisted; (5) no month-over-month variance check, shadow mode unwired; (6) no field classification (unknown = PII,
LLM-visible flag), introspect returns raw sample values; (7) schema/drift.py unwired; (8) all permitted tools sent every
turn, no rate limiter; (9) no data-tier routing or residency fields, no egress retention; (10) evals lack token and
citation metrics; (11) no declarative Zoho/BambooHR profile. Owner-only: ADR 0001 decision, DPA/DPIA, VPC topology.

## Not implemented (owner inputs, already parked)
Live LLM evals, real iCeipts host, NER for names, load testing, Sarvam DPA.
