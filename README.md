# HRMS Agentic Plugin (`hrms-agentic-plugin`)

A standalone, host-agnostic AI microservice that adds an agentic HR copilot to an existing HRMS. It never touches the
host database: it reads through the host's REST API using declarative profiles and approved field mappings, and writes
back only through approved, reversible proposals.

Hosts proven so far: **Frappe HR v15** (live: PF payroll, requisitions, write-back) and **iCeipts** (profile, tested
against fixtures). Jurisdictions for statutory calculations: **India (IN)** and **UAE (AE)**.

---

## What it does

| Area | What is built |
|---|---|
| Chat agent | ReAct loop over a validated tool registry (`/v1/chat`, `/v1/chat/stream`). Tool arguments are schema-checked. Amounts in the answer must come from tool output or the user's message (`UNGROUNDED_AMOUNT`). Only tools matching the intent are offered. At the step limit the agent must give a final answer (`partial: true`) instead of discarding its work. |
| Deterministic tools | Salary structure, EOSB/gratuity, final settlement (IN, AE); leave, travel anomaly, ATS scoring, JD bias check, offer evaluation, onboarding documents, F&F timeline, expense audit. Numbers are computed in code, never by the LLM. Missing inputs are errors, not defaults. |
| Payroll (PF) | `/v1/payroll/pf/calculate`: canonical salary cross-checks (basic ≤ gross, currency matches jurisdiction), results stored with provenance, month-over-month variance review. |
| Schema adaptation | Introspect OpenAPI / Swagger / JSON samples (structure only, no raw values), synthesize mappings, approve mapping versions with per-field data classes (PII / COMPENSATION / INTERNAL / PUBLIC; unclassified fields count as PII). Host record shapes are checked for drift (`409 SCHEMA_DRIFT` until the new shape is approved). |
| Write-back | Proposal → four-eyes approval → rollback (`/v1/writeback/*`). |
| Governance | Per-tenant LLM policy: allowed providers, data tier, provider region, daily token budget, requests per minute (`429 RATE_LIMITED`), egress log with retention and per-tenant deletion. PII is redacted before any prompt. |
| Voice | Sarvam STT/TTS (`/v1/voice/*`) under the same tenant policy: per-tenant opt-in, egress logged, PII gate. Returns `503 VOICE_NOT_CONFIGURED` without a key. |
| MCP | `HrmsMcpServer`: an adapter over the same tool registry. Tenant and jurisdiction come from the caller's auth, never from arguments. |
| Copilot SDK | `sdk/copilot.js`: embeddable widget (Shadow DOM, route/record context via `postMessage`). |
| Evals | `hrms_plugin/evals`: tokens per case, citation grounding, pass rate. Live runs need explicit approval (`HRMS_EVAL_LIVE_APPROVED`). |

Evidence-backed status (implemented / partial / parked):
[`docs/superpowers/plans/2026-10-29-implementation-status.md`](docs/superpowers/plans/2026-10-29-implementation-status.md).

### Known limits

- Compliance diagnose/dashboard endpoints are demo-only. They return `503 COMPLIANCE_SOURCE_NOT_CONNECTED` unless
  `HRMS_DEMO_ENDPOINTS=true`, and demo answers carry `data_source: DEMO_SAMPLE`.
- Requisition archetypes are templates (`source: TEMPLATE`, `requires_review: true`); pay and benefits are left empty.
- Statutory tools refuse jurisdictions other than IN/AE (`JURISDICTION_NOT_SUPPORTED`).
- `hrms_plugin/connectors/` is not used by production code; it is kept on purpose.
- Not done yet: Zoho Recruit profile (needs a tenant's field list), live LLM evals, name NER, load testing.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export HRMS_TENANT_REGISTRY=path/to/tenants.yaml    # format: tests/fixtures/tenant_registry.yaml
export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...  # or openai, gemini, groq, openrouter, nvidia
python -m hrms_plugin.server                         # HOST=127.0.0.1, PORT=8000 by default
```

`GET /healthz` is open. Every other route needs a bearer JWT from an issuer in the tenant registry. Token roles are
mapped per tenant to `SUPER_ADMIN`, `HR_ADMIN`, `PAYROLL_ADMIN`, `HIRING_MANAGER`, `DEPARTMENT_MANAGER` or `EMPLOYEE`.

Docker: `deploy/docker-compose.yml` (set `HRMS_TENANT_REGISTRY_FILE`). Kubernetes manifests: `deploy/k8s/`.

### Configuration

| Variable | Purpose |
|---|---|
| `HRMS_TENANT_REGISTRY` | Tenant YAML: issuer, JWKS, audience, claim names, role map, allowed origins, `llm_policy` |
| `HRMS_HOSTS` | Host connections (base URL, profile, credentials) |
| `HRMS_PAYROLL_RULES` | Payroll rules file (PF rates, ceilings, `variance_review_percent`) |
| `HRMS_DATA_DIR` | Local data directory (default `.hrms_data`, created with mode `0700`) |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_EFFORT`, `LLM_PROVIDER_REGION` | LLM gateway selection |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`), `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `NVIDIA_API_KEY` | Provider keys |
| `SARVAM_API_KEY` | Voice; voice is off without it |
| `HRMS_DEMO_ENDPOINTS` | `true` enables the demo compliance endpoints |
| `HRMS_ENABLE_API_DOCS` | `true` serves the OpenAPI docs |

Per-tenant `llm_policy` keys: `allowed_providers`, `max_data_tier`, `region`, `daily_token_budget`,
`requests_per_minute`, `egress_retention_days`, `tokenize_compensation`, `voice_providers`.

`max_data_tier` defaults to 1 (schema field names only). Chat and the agents send pseudonymised operational data
(tier 2), so a tenant needs `max_data_tier: 2` to use them.

### CLI

```bash
python -m hrms_plugin.cli introspect ...
python -m hrms_plugin.cli calculate-eosb ...
python -m hrms_plugin.cli calculate-salary ...
python -m hrms_plugin.cli audit-compliance --file employees.json   # exits 2 on a missing or invalid file
python -m hrms_plugin.cli check-bias ...
```

`python -m hrms_plugin.cli <command> --help` lists each command's arguments.

---

## HTTP API

| Group | Routes |
|---|---|
| Chat | `POST /v1/chat`, `POST /v1/chat/stream`. Messages over 8,000 characters get `422 MESSAGE_TOO_LONG`. History is trimmed oldest-first and the response reports `history_trimmed`. |
| Schema | `POST /v1/schema/introspect`, `/synthesize`, `/synthesize/agentic`; `GET /v1/schema/mappings`, `/mappings/{vendor}/{entity_type}`; `POST .../versions/{version}/approve`; fingerprint approve and quarantine |
| Hosts | `GET /v1/hosts/requisitions`, `POST /v1/hosts/requisitions/shape/approve` |
| Payroll | `POST /v1/payroll/pf/calculate`, `GET /v1/payroll/pf/results/{employee_id}/{period}` |
| Write-back | `POST /v1/writeback/proposals`, `.../{id}/approve`, `.../{id}/rollback` |
| Governance | `GET` and `DELETE /v1/governance/llm-egress` |
| Voice | `POST /v1/voice/transcribe`, `/synthesize`, `/turn` |
| Other | `POST /v1/remediate/execute`, `POST /v1/requisitions/generate-archetypes`, compliance demo routes, legacy `/api/*` routes |

---

## Project structure

```
hrms_plugin/
├── agents/        # supervisor, orchestrator (ReAct loop), tool registry, domain agents, voice
├── ai/            # LLM gateway: providers, tenant policy checks, per-task caps, usage accounting
├── governance/    # LLM policy, egress log, rate limiter
├── integrations/  # declarative host profiles (frappe.yaml, iceipts.yaml), mapping overlay, shape checks
├── payroll/       # canonical salary facts, PF, rules, results service
├── schema/        # introspector, synthesizer, prober, drift
├── security/      # JWT/JWKS, RBAC, tenant registry, PII redaction, audit
├── storage/       # memory and SQLite stores, mapping store with field classification
├── writeback/     # proposal, approval, rollback
├── server/        # FastAPI app, MCP adapter, entry point
├── evals/         # eval cases and runner
└── connectors/    # older REST connectors (kept, not used by production code)
sdk/               # embeddable copilot widget
deploy/            # Dockerfile, docker-compose, k8s
docs/              # plans, specs, ADRs, KT notes
tests/             # unit, contract, statutory, drift, integration, e2e
```

---

## Testing

```bash
pytest                         # full suite
pytest --cov=hrms_plugin       # with coverage
ruff check hrms_plugin tests
```

Latest run on `agentic-ai`: **2068 passed, 0 failed, 19 skipped**; line coverage 98.9%. The 19 skipped tests need a
live Frappe sandbox. The work was built test-first, with each failing test recorded before its fix. Each batch was
checked by mutation sampling and by an independent verification pass. The TDD logs and findings are in
[`docs/superpowers/plans/`](docs/superpowers/plans/).

---

## Documentation

- [`docs/PHASE_WISE_PLAN.md`](docs/PHASE_WISE_PLAN.md): original phase-wise plan and testing strategy
- [`docs/KT_DOCUMENTATION.md`](docs/KT_DOCUMENTATION.md): knowledge-transfer notes
- [`docs/adr/`](docs/adr/): architecture decisions (ADR 0001, build vs buy, awaits an owner decision)
- [`docs/superpowers/specs/`](docs/superpowers/specs/): design specs for each piece (auth, data protection, SDK
  security, LLM layer, Frappe PF, write-back, governance, cost and evals, second vendor, token waste)
- [`docs/superpowers/plans/`](docs/superpowers/plans/): implementation plans, status audit, TDD logs, findings
