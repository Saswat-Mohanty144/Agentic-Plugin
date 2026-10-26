# HRMS Agentic Plugin — Knowledge Transfer (KT) Documentation

> **Version**: 1.0.0 &nbsp;|&nbsp; **Python**: ≥ 3.11 &nbsp;|&nbsp; **Build**: hatchling &nbsp;|&nbsp; **License**: MIT  
> **Last Updated**: 2026-10-26

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Overview](#2-architecture-overview)
3. [Repository Structure](#3-repository-structure)
4. [Core Subsystems (Deep Dive)](#4-core-subsystems-deep-dive)
   - 4.1 [Schema Engine](#41-schema-engine---the-5-stage-pipeline)
   - 4.2 [Agent Fleet](#42-agent-fleet---cognitive-domain-specialists)
   - 4.3 [Connector Layer](#43-connector-layer---host-agnostic-rest-client)
   - 4.4 [Security & Governance](#44-security--governance)
   - 4.5 [AI / LLM Gateway](#45-ai--llm-gateway)
   - 4.6 [RAG Knowledge Base](#46-rag---statutory-legal-knowledge-base)
   - 4.7 [Storage Layer](#47-storage-layer)
   - 4.8 [Server (API + MCP)](#48-server-api-surface--mcp)
   - 4.9 [CLI](#49-cli)
   - 4.10 [SDK (Embeddable Copilot)](#410-sdk---embeddable-copilot-widget)
5. [Data Flow Diagrams](#5-data-flow-diagrams)
6. [Key Design Decisions & Invariants](#6-key-design-decisions--invariants)
7. [Environment & Configuration](#7-environment--configuration)
8. [Deployment](#8-deployment)
9. [Testing Strategy (5-Tier Pyramid)](#9-testing-strategy-5-tier-pyramid)
10. [Common Development Workflows](#10-common-development-workflows)
11. [Gotchas & Known Limitations](#11-gotchas--known-limitations)
12. [Onboarding Checklist](#12-onboarding-checklist)
13. [Glossary](#13-glossary)

---

## 1. Executive Summary

`hrms-agentic-plugin` is a **standalone, host-agnostic intelligence microservice** that bolts autonomous AI capabilities onto any HRMS platform (iceipts_hrms, Frappe/ERPNext, BambooHR, Workday, Zoho People, Darwinbox, or custom REST APIs) **without touching the host database or modifying core application code**.

### What it does

| Capability | Description |
|---|---|
| **Dynamic Schema Adaptation** | Auto-discovers OpenAPI/Swagger specs or JSON samples → synthesizes bi-directional FieldMap bindings to Canonical models |
| **14 Domain Agents** | Recruitment, Payroll, Compliance, Leave, Attendance, Onboarding, Offboarding, Travel & Expense, Voice AI, and more |
| **Statutory Compliance RAG** | Searchable corpus of labor laws (India, UAE, Saudi Arabia, US) with gazette citations |
| **Zero-Trust PII Masking** | All data redacted before hitting LLMs, re-hydrated on egress |
| **Embeddable Copilot SDK** | Single `<script>` tag (Shadow DOM, < 120KB gzipped) for host UIs |
| **MCP Server** | Model Context Protocol surface for external AI assistants |

### Who should read this

- New developers inheriting or extending this plugin
- DevOps engineers deploying/operating the service
- Architects evaluating integration with a host HRMS

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        HOST HRMS (Any Vendor)                           │
│    iceipts_hrms  ·  Frappe/ERPNext  ·  BambooHR  ·  Workday  ·  Zoho  │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ REST / OpenAPI
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      HRMS AGENTIC PLUGIN (:8000)                        │
│                                                                          │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────────────┐   │
│  │   CLI    │  │  FastAPI App │  │        MCP Server                │   │
│  │          │  │  /v1/chat    │  │  hrms_query_labor_statutes       │   │
│  │introspect│  │  /v1/voice   │  │  hrms_calculate_payroll_ctc      │   │
│  │calc-eosb │  │  /v1/schema  │  │  hrms_audit_compliance           │   │
│  │calc-sal  │  │  /v1/remediate│ │  hrms_score_candidate_ats        │   │
│  │audit     │  │  /healthz    │  │  ... (8 tools total)             │   │
│  └──────────┘  └──────┬───────┘  └──────────────────────────────────┘   │
│                        │                                                 │
│  ┌─────────────────────▼─────────────────────────────────────────────┐   │
│  │              SUPERVISOR AGENT (Intent Router)                     │   │
│  │   classify_intent() → route to specialist → unified response     │   │
│  │                           │                                       │   │
│  │   ┌──────────────────┐   ┌▼────────────────────┐                 │   │
│  │   │ process_message()│   │ process_agentic()    │                 │   │
│  │   │ (deterministic)  │   │ (ReAct tool loop)    │                 │   │
│  │   └──────────────────┘   └──────────────────────┘                 │   │
│  └──────────┬────────────────────────────────────────────────────────┘   │
│             │                                                            │
│  ┌──────────▼──────────────────────────────────────────────────────┐     │
│  │              DOMAIN SPECIALIST AGENTS                            │     │
│  │                                                                  │     │
│  │  StatutoryPayroll  ·  Compliance  ·  Recruitment  ·  Leave      │     │
│  │  Onboarding  ·  Offboarding  ·  TravelExpense  ·  Voice        │     │
│  └──────────┬──────────────────────────────────────────────────────┘     │
│             │                                                            │
│  ┌──────────▼─────────┐  ┌──────────────────┐  ┌──────────────────┐     │
│  │  Schema Engine      │  │  Security Layer  │  │  LLM Gateway     │     │
│  │  Introspector       │  │  PII Masking     │  │  Multi-Provider  │     │
│  │  Synthesizer        │  │  RBAC            │  │  Tool Calling    │     │
│  │  Prober             │  │  Audit Headers   │  │  Streaming SSE   │     │
│  │  Drift Detector     │  │  Vault Tokens    │  │  Fake (offline)  │     │
│  │  Mapping Engine     │  └──────────────────┘  └──────────────────┘     │
│  └──────────┬─────────┘                                                  │
│             │                                                            │
│  ┌──────────▼─────────┐  ┌──────────────────┐  ┌──────────────────┐     │
│  │  Connector Layer    │  │  Storage Layer   │  │  RAG / Legal KB  │     │
│  │  DynamicREST        │  │  SQLite KV Store │  │  Statutory       │     │
│  │  VendorProfiles     │  │  Mapping Store   │  │  Citations       │     │
│  │  CircuitBreaker     │  │  Vault Store     │  │  4 Jurisdictions │     │
│  │  Rate Limiter       │  │  Memory Store    │  └──────────────────┘     │
│  │  OAuth2 Token Mgr   │  └──────────────────┘                          │
│  └─────────────────────┘                                                 │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│          SDK / Copilot Widget (sdk/copilot.js + index.html)             │
│          Shadow DOM · <120KB gzipped · postMessage context sync          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Structure

```
hrms-agentic-plugin/
├── .env                           # Local environment vars (gitignored in prod)
├── pyproject.toml                 # Hatchling build, deps, pytest, ruff config
├── README.md                      # Project overview & capability summary
│
├── docs/
│   ├── PHASE_WISE_PLAN.md         # Master 6-phase implementation roadmap
│   ├── README.md                  # Docs index
│   └── KT_DOCUMENTATION.md       # ← This file
│
├── hrms_plugin/                   # Core Python package
│   ├── __init__.py                # Package metadata (__version__ = "1.0.0")
│   ├── cli.py                     # CLI entrypoint (hrms-plugin command)
│   │
│   ├── schema/                    # 5-Stage Dynamic Schema Adaptation Engine
│   │   ├── canonical.py           # Vendor-neutral domain models (10 entity types)
│   │   ├── introspector.py        # OpenAPI / Swagger / JSON sample discovery
│   │   ├── synthesizer.py         # Semantic field matching → FieldMap generation
│   │   ├── mapping.py             # Declarative FieldMap & EntityMapping engine
│   │   ├── prober.py              # Shadow verification & quality scoring
│   │   └── drift.py               # Runtime drift detection & self-healing
│   │
│   ├── agents/                    # Cognitive Multi-Agent Fleet
│   │   ├── __init__.py            # Unified re-exports for all agents
│   │   ├── supervisor.py          # Intent Router & Orchestration Brain
│   │   ├── orchestrator.py        # ReAct loop (Think→Act→Observe→Reflect)
│   │   ├── tools.py               # AgenticToolRegistry (tool definitions)
│   │   ├── statutory_payroll.py   # India CTC, UAE EOSB, salary structuring
│   │   ├── compliance.py          # Labor law violation auditing
│   │   ├── recruitment.py         # ATS scoring, JD bias detection
│   │   ├── leave_attendance.py    # Leave evaluation, impossible travel
│   │   ├── onboarding.py          # Offer evaluation, KYC, IT provisioning
│   │   ├── offboarding.py         # FNF settlement, exit clearance
│   │   ├── travel_expense.py      # Expense claim auditing
│   │   └── voice.py               # Sarvam AI voice integration
│   │
│   ├── connectors/                # Host-Agnostic REST Client Layer
│   │   ├── base.py                # ABC + exception hierarchy
│   │   ├── dynamic_rest.py        # Live CRUD against any HRMS
│   │   ├── auth.py                # ApiKey, Basic, OAuth2 token manager
│   │   ├── concurrency.py         # Idempotency keys, optimistic locking
│   │   ├── resilience.py          # Circuit breaker, rate limiter
│   │   ├── compensation.py        # Compensation connector helpers
│   │   └── profiles/              # Vendor-specific adapters
│   │       ├── base_profile.py    # VendorProfile ABC
│   │       ├── generic.py         # Generic REST profile
│   │       ├── iceipts.py         # iceipts_hrms adapter
│   │       ├── frappe.py          # Frappe/ERPNext adapter
│   │       ├── bamboohr.py        # BambooHR adapter
│   │       ├── workday.py         # Workday adapter
│   │       ├── zoho.py            # Zoho People adapter
│   │       └── dynamic.py         # Auto-detecting profile
│   │
│   ├── ai/
│   │   └── llm.py                 # Multi-provider LLM gateway (795 lines)
│   │
│   ├── rag/
│   │   └── store.py               # Statutory knowledge base (IN/AE/SA/US)
│   │
│   ├── security/
│   │   ├── masking.py             # PII regex detection & vault tokenization
│   │   ├── rbac.py                # RBAC roles, permissions, separation of duties
│   │   └── audit.py               # Host audit attribution headers
│   │
│   ├── storage/
│   │   ├── base.py                # KeyValueStore ABC
│   │   ├── memory_store.py        # In-memory ephemeral store
│   │   ├── sqlite_store.py        # Persistent SQLite-backed store
│   │   ├── mapping_store.py       # EntityMapping serialization/persistence
│   │   └── vault_store.py         # PII vault token persistence
│   │
│   └── server/
│       ├── app.py                 # FastAPI application (607 lines, 20+ routes)
│       └── mcp_server.py          # Model Context Protocol server (8 tools)
│
├── sdk/
│   ├── copilot.js                 # Embeddable copilot widget (Shadow DOM)
│   └── index.html                 # Standalone demo page
│
├── deploy/
│   ├── Dockerfile                 # Multi-stage production build
│   ├── docker-compose.yml         # API server + nginx SDK static serving
│   └── k8s/                       # Kubernetes manifests (Helm-ready)
│
└── tests/                         # 5-Tier Testing Pyramid
    ├── unit/                      # 24 test files (agents, schema, security, …)
    ├── contract/                  # DynamicREST connector + iceipts adaptation
    ├── statutory/                 # Decimal-exact payroll calculations
    ├── drift/                     # Schema drift detection scenarios
    └── e2e/                       # Full copilot-to-agent E2E flow
```

---

## 4. Core Subsystems (Deep Dive)

### 4.1 Schema Engine — The 5-Stage Pipeline

The schema engine is the plugin's core differentiator. It allows the plugin to connect to **any** HRMS without hardcoded field mappings.

**Files**: `hrms_plugin/schema/`

#### Stage 1: Introspection (`introspector.py`)

| Class | Purpose |
|---|---|
| `SchemaIntrospector` | Parses OpenAPI 3.x / Swagger 2.0 specs (YAML/JSON) or raw JSON record samples |
| `DiscoveredEntity` | Represents a discovered host entity with fields, endpoints, and ID resolution |
| `DiscoveredField` | Individual field metadata: name, data type, nullability, enums, samples |
| `IntrospectionResult` | Aggregation of all discovered entities from a spec |

**Key methods**:
- `introspect_file(path)` — Parse a local spec file
- `introspect_openapi(spec_dict)` — Parse an in-memory spec dict
- `introspect_json_samples(name, samples)` — Infer schema from JSON payloads

#### Stage 2: Synthesis (`synthesizer.py`)

| Class | Purpose |
|---|---|
| `MappingSynthesizer` | Matches discovered fields → canonical fields using synonym dictionaries, normalized token matching, and confidence scoring |
| `CANONICAL_SPECIFICATIONS` | Master spec dict defining synonyms, transforms, and enum maps for each `EntityType` (862 lines) |
| `SynthesisReport` | Synthesis results: mapped/unmapped fields, average confidence, writeback count |

**Matching priority** (descending confidence):
1. **Compound name match** (e.g., `first_name + last_name → full_name`) — 0.95
2. **Exact synonym match** (case-insensitive) — 1.0 / 0.95
3. **Normalized token equivalence** (camelCase → snake_case normalization) — 0.90
4. **Normalized synonym match** — 0.85
5. **Substring/prefix match** (≥4 chars) — 0.75

**Agentic synthesis** (`synthesize_agentic`): Falls back to LLM reasoning for unmapped ambiguous fields.

#### Stage 3: Mapping (`mapping.py`)

| Class | Purpose |
|---|---|
| `FieldMap` | Maps one canonical field from one or more vendor paths with transforms |
| `EntityMapping` | Complete vendor-to-canonical mapping for one entity type |
| `resolve_path()` | Sub-millisecond dotted-path traversal with array projection (`items[].name`) |
| `TRANSFORMS` | Registry of transform functions: `string`, `strip`, `decimal`, `datetime`, `date`, `list`, `join_names`, etc. |

**Bidirectional**: `to_canonical()` reads from vendor → canonical; `to_vendor()` writes canonical → vendor wire format.

#### Stage 4: Probing (`prober.py`)

| Class | Purpose |
|---|---|
| `ShadowProber` | Dry-runs candidate mappings against sample records |
| `ProbeReport` | Quality score (0.0–1.0), issues (ERROR/WARNING/INFO), projected records |

**Scoring**: Each ERROR deducts 0.25, each WARNING deducts 0.05. Final score clamped to [0.0, 1.0].

#### Stage 5: Drift Detection (`drift.py`)

| Class | Purpose |
|---|---|
| `SchemaDriftInterceptor` | Monitors runtime payloads for unmapped fields or HTTP 400 schema rejections |
| `DriftEvent` | Records drift with severity (INFO/WARNING/CRITICAL) and suggested patch |
| `generate_remediation_delta()` | Produces a patch dict to update FieldMap without manual code changes |

#### Canonical Models (`canonical.py`)

10 vendor-neutral domain dataclasses — all monetary fields use `Decimal`, never `float`:

| Model | Key Fields |
|---|---|
| `CanonicalEmployee` | `full_name`, `work_email`, `joining_date`, `salary` (Decimal), `department` |
| `CanonicalCandidate` | `full_name`, `email`, `resume_text`, `skills`, `years_experience` (Decimal) |
| `CanonicalRequisition` | `title`, `department`, `headcount`, `must_have_skills`, `min_years_experience` (Decimal) |
| `CanonicalLeaveRequest` | `employee_ref`, `leave_type`, `start_date`, `end_date`, `total_days` (Decimal) |
| `CanonicalLeaveBalance` | `allocated_days`, `used_days`, `remaining_days` (all Decimal) |
| `CanonicalPunch` | `punch_time`, `punch_type` (IN/OUT), `latitude`/`longitude` (Decimal) |
| `CanonicalPayslip` | `basic_salary`, `gross_salary`, `net_pay`, `total_deductions` (all Decimal) |
| `CanonicalExpenseClaim` | `claim_type`, `amount` (Decimal), `receipt_urls` |
| `CanonicalAttendanceRequest` | `request_date`, `reason`, `status` |
| `CanonicalAsset` | `asset_tag`, `asset_type`, `serial_number`, `assigned_to` |

Every model carries a `SourceRef` (vendor + entity_type + external_id) and `raw: Dict` for round-tripping unmodelled vendor fields. PII-bearing models expose `pii_literals()` for the masking gateway.

---

### 4.2 Agent Fleet — Cognitive Domain Specialists

**Files**: `hrms_plugin/agents/`

#### Supervisor (`supervisor.py`)
The brain of the system. Two execution modes:

| Method | Mode | Description |
|---|---|---|
| `classify_intent(msg)` | Sync | Keyword-heuristic intent classification → `UserIntent` enum + confidence |
| `process_message(msg)` | Sync (deterministic) | Routes to specialist agent, returns `AgentResponse` |
| `process_agentic(msg)` | Async (ReAct) | Multi-turn LLM tool loop via `AgenticOrchestrator` |
| `stream_agentic(msg)` | Async (SSE) | Real-time streaming events for UI |

**12 Intent Types**: `PAYROLL_STRUCTURE`, `PAYROLL_EOSB`, `COMPLIANCE_AUDIT`, `LEAVE_APPLY`, `ATTENDANCE_ANOMALY`, `RECRUITMENT_ATS`, `RECRUITMENT_BIAS`, `ONBOARDING`, `OFFBOARDING_FNF`, `TRAVEL_EXPENSE`, `LEGAL_QNA`, `GENERAL_HR`

#### Orchestrator (`orchestrator.py`)
Implements the **ReAct** (Reasoning + Acting) pattern:

```
Think → Act (Tool Call) → Observe → Reflect → Act → ... → Final Answer
```

- Max 5 iterations per request (configurable)
- Automatically extracts statutory citations from tool outputs
- Collects remediation actions from compliance violations
- Supports full SSE streaming via `run_stream()`

#### Domain Specialist Agents

| Agent | File | Key Capabilities |
|---|---|---|
| **StatutoryPayrollAgent** | `statutory_payroll.py` | India CTC breakdown (EPF/ESI/HRA/Gratuity), UAE salary structuring, UAE EOSB (Article 51) — all `Decimal` arithmetic |
| **ComplianceAgent** | `compliance.py` | Multi-jurisdiction labor law auditing (UAE: probation, WPS, working hours; India: PF/ESI; SA/US rules) with violation-level remediation patches |
| **RecruitmentAgent** | `recruitment.py` | ATS candidate scoring (skill/experience/education rubric, composite 0–100), JD bias detection with inclusive rewrite suggestions, duplicate candidate merging |
| **LeaveAttendanceAgent** | `leave_attendance.py` | Leave request evaluation against balance, impossible travel detection (Haversine + velocity analysis) |
| **OnboardingAgent** | `onboarding.py` | Offer evaluation with band comparison, KYC document verification (PAN/Aadhaar/Emirates ID), IT provisioning plan generation |
| **OffboardingAgent** | `offboarding.py` | Final settlement (FNF) computation (gratuity + leave encashment + pro-rata salary − deductions), UAE Article 53 timeline audit, exit clearance evaluation |
| **TravelExpenseAgent** | `travel_expense.py` | Expense claim auditing with per-diem limits by tier (METRO/NON_METRO), category caps (MEAL/HOTEL/FLIGHT), pre-authorization checks |
| **VoiceAgent** | `voice.py` | Sarvam AI STT/TTS integration (Saaras/Bulbul models); requires `SARVAM_API_KEY` or fails loudly |

#### Tool Registry (`tools.py`)
The `AgenticToolRegistry` wraps all specialist agents as callable tools for the ReAct orchestrator. Each tool has:
- JSON schema definition (OpenAI function calling format)
- Async `execute(name, arguments)` dispatcher
- Automatic result serialization

---

### 4.3 Connector Layer — Host-Agnostic REST Client

**Files**: `hrms_plugin/connectors/`

#### Base Layer (`base.py`)

Abstract `BaseHRMSConnector` with 5 operations:
- `fetch_entity()`, `list_entities()`, `create_entity()`, `update_entity()`, `delete_entity()`

**Exception hierarchy**:
```
HRMSConnectorError
├── HRMSAuthError        (401/403)
├── HRMSNotFoundError    (404)
├── HRMSValidationError  (400/422)
├── HRMSConflictError    (409)
└── HRMSTimeoutError     (timeout)
```

#### Dynamic REST Connector (`dynamic_rest.py`)

The production connector. Driven by `EntityMapping` and `VendorProfile`:

1. **Reads**: GET → vendor response → `VendorProfile.unwrap_response()` → `EntityMapping.to_canonical()` → `CanonicalEntity`
2. **Writes**: Canonical payload → `VendorProfile.normalize_payload_for_host()` → POST/PATCH → idempotency check → self-healing on 400 errors

**Enterprise features**:
- Rate limiting via `TokenBucketRateLimiter`
- Circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED)
- Idempotency keys (SHA-256 of `tenant:entity_type:action:actor:payload`)
- Automatic retry with exponential backoff (429, 502, 503, 504)
- Self-healing: On HTTP 400, `heal_payload_from_error()` patches the wire payload and retries once

#### Auth Strategies (`auth.py`)

| Strategy | Use Case |
|---|---|
| `ApiKeyAuth` | Static API key/bearer token |
| `BasicAuth` | HTTP Basic (base64 username:password) |
| `OAuth2TokenManager` | Client credentials / refresh token flow with proactive rotation |

#### Vendor Profiles (`connectors/profiles/`)

Each profile adapts endpoint paths, response unwrapping, error extraction, and payload normalization for a specific host:

| Profile | File | Host HRMS |
|---|---|---|
| `IceiptsProfile` | `iceipts.py` | iceipts_hrms (10 entity types, full CRUD) |
| `FrappeProfile` | `frappe.py` | Frappe/ERPNext |
| `BambooHRProfile` | `bamboohr.py` | BambooHR |
| `WorkdayProfile` | `workday.py` | Workday |
| `ZohoProfile` | `zoho.py` | Zoho People |
| `GenericProfile` | `generic.py` | Any OpenAPI-compliant REST API |
| `DynamicProfile` | `dynamic.py` | Auto-detecting (introspection-driven) |

#### Resilience (`resilience.py`)

| Component | Description |
|---|---|
| `TokenBucketRateLimiter` | Async token-bucket with configurable `rate` (req/s) and `capacity` (burst) |
| `CircuitBreaker` | 3-state (CLOSED/OPEN/HALF_OPEN) with configurable failure threshold (default 5), recovery timeout (30s), and half-open success threshold (2) |
| `CircuitBreakerOpenError` | Fast-fail exception with `retry_after` seconds |

#### Concurrency (`concurrency.py`)

| Component | Description |
|---|---|
| `compute_idempotency_key()` | Deterministic SHA-256 hash of mutation context |
| `IdempotencyManager` | In-memory deduplicator with TTL (default 120s), per-key locking |
| `OptimisticConcurrencyManager` | ETag/version verification before updates |

---

### 4.4 Security & Governance

**Files**: `hrms_plugin/security/`

#### PII Masking (`masking.py`)

`PiiMaskingGateway` — Pre-prompt PII redaction engine:

- **Regex patterns**: AADHAAR (`XXXX XXXX XXXX`), PAN (`ABCDE1234F`), Emirates ID, SSN, Email, Phone
- **Vault tokenization**: Replaces PII with deterministic `[VAULT_TYPE_HEXID]` tokens
- **Record masking**: Deep-walks dictionaries, masks sensitive keys (`salary`, `bank_account`, `aadhaar`, etc.)
- **Reversible**: `unmask_text()` re-hydrates tokens from in-memory vault or persistent store
- **Tenant-scoped**: All tokens scoped by `tenant_id` for multi-tenant isolation

#### RBAC (`rbac.py`)

6 roles × 10 permissions with static policy matrix:

| Role | Permissions |
|---|---|
| `SUPER_ADMIN` | All |
| `HR_ADMIN` | All except `MUTATE_SALARY`, `RUN_PAYROLL` |
| `PAYROLL_ADMIN` | View, salary, payroll, compliance |
| `HIRING_MANAGER` | View employee, manage recruitment |
| `DEPARTMENT_MANAGER` | View, apply/approve leave |
| `EMPLOYEE` | View own, apply leave |

**Separation of duties**: Self-approval prevention for leave and salary mutations.

#### Audit Attribution (`audit.py`)

`HostAuditGateway` generates tamper-proof HTTP headers for every host write:

| Header | Value |
|---|---|
| `X-Agent-Invocation-Id` | Unique invocation UUID |
| `X-Agent-Name` | Agent that executed the action |
| `X-HITL-Approver-Id` | Human approver's user ID |
| `X-Reasoning-Hash` | SHA-256 of the reasoning payload |
| `X-Tenant-Id` | Tenant scope |
| `X-Trace-Timestamp` | UTC ISO timestamp |

---

### 4.5 AI / LLM Gateway

**File**: `hrms_plugin/ai/llm.py` (795 lines)

Multi-provider abstraction supporting:

| Provider | Key |
|---|---|
| **Fake** (default) | Offline deterministic responses for testing |
| **OpenAI** | GPT-4 / GPT-3.5 |
| **Groq** | Fast inference |
| **OpenRouter** | Routing hub |
| **Anthropic** | Claude Messages API |
| **Gemini** | Google Gemini REST API |
| **NVIDIA NIM** | Local/enterprise inference |

**Key invariants**:
- All prompts pass through `PiiMaskingGateway` before leaving the process
- `TaskClass.SCORING` and `TaskClass.EVALUATION` → temperature pinned to 0.0
- Tool calling (function definitions) supported for ReAct orchestration
- Streaming SSE via `chat_stream()` for real-time token delivery
- `LLM_PROVIDER` env var selects provider; defaults to `fake` for offline mode

---

### 4.6 RAG — Statutory Legal Knowledge Base

**File**: `hrms_plugin/rag/store.py`

`StatutoryKnowledgeBase` — In-memory hybrid retriever with:

- **Jurisdiction filtering**: `IN` (India), `AE` (UAE), `SA` (Saudi Arabia), `US` (USA), `GLOBAL`
- **Keyword scoring**: Tokenized query matching with weighted scoring (act name: 3x, title: 2x, section: 2.5x, content: 1x)
- **Corpus**: 10 `LegalCitation` entries covering:

| Jurisdiction | Statutes |
|---|---|
| **India** | Code on Wages 2019, EPF Act 1952, ESI Act 1948, Gratuity Act 1972, Maternity Benefit Act 1961 |
| **UAE** | Federal Decree-Law No. 33/2021 (probation, hours), Article 51 (EOSB), WPS Resolution 43/2022 |
| **Saudi Arabia** | Labour Law Royal Decree M/51 (hours, overtime, EOSB) |
| **USA** | FLSA (overtime, minimum wage) |

Each citation includes:
- `statutory_rules` dict with machine-readable thresholds (used by compliance agent)
- `penalty_or_consequence` (human-readable)
- `official_gazette_ref` for auditable provenance

---

### 4.7 Storage Layer

**Files**: `hrms_plugin/storage/`

| Store | Backend | Purpose |
|---|---|---|
| `MemoryKeyValueStore` | Python dict | Ephemeral testing |
| `SqliteKeyValueStore` | SQLite file (`.hrms_data/plugin_storage.db`) | Persistent storage |
| `MappingStore` | Wraps KV store | Serialize/deserialize `EntityMapping` objects (keyed by `tenant:vendor:entity_type`) |
| `PiiVaultStore` | Wraps KV store | Persist PII vault tokens across restarts |

---

### 4.8 Server (API Surface + MCP)

#### FastAPI App (`server/app.py`)

**Port**: 8000 (configurable via `PORT` env var)

| Route | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Health check (version, jurisdictions) |
| `/v1/chat` | POST | Unified copilot endpoint (PII masking, agentic/deterministic mode) |
| `/v1/chat/stream` | POST | Real-time SSE event stream |
| `/v1/voice/transcribe` | POST | STT via Sarvam AI |
| `/v1/voice/synthesize` | POST | TTS via Sarvam AI |
| `/v1/voice/turn` | POST | Full-duplex: Audio → STT → Agent → TTS |
| `/v1/schema/introspect` | POST | Introspect OpenAPI spec or JSON samples |
| `/v1/schema/synthesize` | POST | Synthesize FieldMap (deterministic) |
| `/v1/schema/synthesize/agentic` | POST | Synthesize FieldMap (LLM-enhanced) |
| `/v1/schema/mappings` | GET | List persisted mappings for tenant |
| `/v1/schema/mappings/{vendor}/{entity_type}` | GET | Get specific mapping |
| `/v1/remediate/execute` | POST | Execute human-approved compliance remediation |
| `/v1/compliance/diagnose` | GET | Live compliance diagnosis |
| `/v1/compliance/dashboard/live` | GET | Statutory setup matrix |
| `/api/compliance/agent/chat` | POST | Legacy adapter for Next.js |
| `/api/requisitions/generate-archetypes` | POST | Generate job requisition archetypes |

**Multi-tenancy**: `X-Tenant-Id` header scopes all operations.

#### MCP Server (`server/mcp_server.py`)

8 registered MCP tools:

| Tool | Purpose |
|---|---|
| `hrms_query_labor_statutes` | Search labor law citations |
| `hrms_calculate_payroll_ctc` | Gross-to-net salary breakdown |
| `hrms_calculate_uae_eosb` | UAE Article 51 gratuity |
| `hrms_audit_compliance` | Statutory violation audit |
| `hrms_score_candidate_ats` | Resume scoring |
| `hrms_audit_job_description` | JD bias detection |
| `hrms_evaluate_leave_request` | Leave evaluation |
| `hrms_detect_impossible_travel` | GPS anomaly detection |

---

### 4.9 CLI

**File**: `hrms_plugin/cli.py`

```bash
hrms-plugin introspect <schema_path>        # Parse OpenAPI spec
hrms-plugin calculate-eosb --basic 15000 --tenure 3.5
hrms-plugin calculate-salary --ctc 1200000 --jurisdiction IN
hrms-plugin audit-compliance --file employees.json --jurisdiction AE
hrms-plugin check-bias --text "Looking for a young, aggressive rockstar..."
```

---

### 4.10 SDK — Embeddable Copilot Widget

**Files**: `sdk/copilot.js` (23KB), `sdk/index.html`

- **Shadow DOM**: Renders inside a shadow root to prevent CSS bleeding
- **postMessage API**: Synchronizes route and record context with the host page
- **Bundle target**: < 120KB gzipped
- **Deployment**: Served as static files via nginx (see `docker-compose.yml`)

---

## 5. Data Flow Diagrams

### 5.1 Chat Request Flow

```
User Message
     │
     ▼
[FastAPI /v1/chat]
     │
     ├── PII Masking (if mask_pii=true)
     │   └── PiiMaskingGateway.mask_text()
     │       └── Vault tokens generated
     │
     ▼
[SupervisorAgent]
     │
     ├── classify_intent() → UserIntent + confidence
     │
     ├─── if agentic=false ──────────────────┐
     │    └── process_message()              │
     │        └── Direct specialist call     │
     │        └── Return AgentResponse       │
     │                                       │
     └─── if agentic=true                    │
          └── process_agentic()              │
              └── AgenticOrchestrator.run()  │
                  ├── LLM Chat (tool defs)   │
                  ├── Tool Call → Execute     │
                  ├── Observe result          │
                  ├── Loop (max 5 iters)      │
                  └── Final Answer            │
                                             │
     ◄───────────────────────────────────────┘
     │
     ├── PII Unmasking (if mask_pii=true)
     │   └── PiiMaskingGateway.unmask_text()
     │
     ▼
[AgentResponse JSON]
```

### 5.2 Schema Adaptation Flow

```
Host HRMS API Spec (swagger.json / openapi.yaml)
     │
     ▼
[Stage 1: Introspect]  SchemaIntrospector
     │                  → DiscoveredEntity[]
     ▼
[Stage 2: Synthesize]  MappingSynthesizer
     │                  → EntityMapping + SynthesisReport
     ▼
[Stage 3: Compile]     FieldMap[] with transforms
     │                  → Sub-ms path resolution
     ▼
[Stage 4: Probe]       ShadowProber
     │                  → Quality score 0.0–1.0
     ▼
[Stage 5: Monitor]     SchemaDriftInterceptor
                        → DriftEvent[] + remediation patches
```

---

## 6. Key Design Decisions & Invariants

| # | Decision | Rationale |
|---|---|---|
| 1 | **Decimal for all money** | `float` arithmetic causes rounding errors in statutory calculations. All monetary fields use `Decimal(str(x))` with `ROUND_HALF_UP`. |
| 2 | **Vendor-neutral canonical models** | Agents reason against canonical types only. Vendor quirks are isolated in VendorProfiles. |
| 3 | **PII masking before LLM calls** | Zero-trust: no PII leaves the process boundary without vault tokenization. |
| 4 | **Idempotency keys on every write** | Prevents double-booking from retries. SHA-256 of (tenant, entity, action, actor, payload). |
| 5 | **Circuit breaker on all outbound calls** | Prevents cascading failures when host HRMS is down. |
| 6 | **Fake LLM provider as default** | All tests run offline without API keys. `LLM_PROVIDER=fake` is the default. |
| 7 | **Separation of Duties enforcement** | Self-approval blocked for leave and salary changes at the RBAC layer. |
| 8 | **Statutory citations are mandatory** | Every compliance finding must cite the specific gazette-referenced statute. |
| 9 | **Sarvam AI is the only voice provider** | Voice features require `SARVAM_API_KEY` or fail loudly with declared stubs. |
| 10 | **Shadow DOM for SDK** | Prevents CSS/JS bleeding between the copilot and host page. |

---

## 7. Environment & Configuration

### Required Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `fake` | LLM provider: `fake`, `openai`, `groq`, `openrouter`, `anthropic`, `gemini`, `nvidia` |
| `PORT` | `8000` | FastAPI server port |

### Optional Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | Required if `LLM_PROVIDER=openai` |
| `GROQ_API_KEY` | Required if `LLM_PROVIDER=groq` |
| `OPENROUTER_API_KEY` | Required if `LLM_PROVIDER=openrouter` |
| `ANTHROPIC_API_KEY` | Required if `LLM_PROVIDER=anthropic` |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Required if `LLM_PROVIDER=gemini` |
| `NVIDIA_API_KEY` | Required if `LLM_PROVIDER=nvidia` |
| `SARVAM_API_KEY` | Required for voice features |
| `LLM_MODEL` / `GOOGLE_MODEL` | Model name override |
| `HOST_HRMS_BASE_URL` | Base URL of the host HRMS |
| `DEFAULT_JURISDICTION` | Default jurisdiction code |
| `ENVIRONMENT` | `development` / `production` |
| `LOG_LEVEL` | Python logging level |

---

## 8. Deployment

### Local Development

```bash
# 1. Create venv and install
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"

# 2. Run server
uvicorn hrms_plugin.server.app:app --host 0.0.0.0 --port 8000 --reload

# 3. Test health
curl http://localhost:8000/healthz
```

### Docker

```bash
cd deploy
docker compose up --build
# API: http://localhost:8000
# SDK: http://localhost:8080
```

### Docker Image (standalone)

```bash
docker build -f deploy/Dockerfile -t hrms-agentic-plugin .
docker run -p 8000:8000 -e LLM_PROVIDER=gemini -e GEMINI_API_KEY=xxx hrms-agentic-plugin
```

---

## 9. Testing Strategy (5-Tier Pyramid)

```
                    ┌───────────┐
                    │    E2E    │  1 test file
                    │ (Copilot) │  Full agent loop
                    ├───────────┤
                  ┌─┤   Drift   ├─┐  2 test files
                  │ │ (Runtime) │ │  Schema evolution
                  ├─┼───────────┼─┤
                ┌─┤ │ Statutory │ ├─┐  1 test file
                │ │ │ (Decimal) │ │ │  Payroll exactness
                ├─┼─┼───────────┼─┼─┤
              ┌─┤ │ │ Contract  │ │ ├─┐  2 test files
              │ │ │ │ (HTTP)    │ │ │ │  Connector wire
              ├─┼─┼─┼───────────┼─┼─┼─┤
              │ │ │ │   Unit    │ │ │ │  24 test files
              │ │ │ │ (Fast)    │ │ │ │  Core logic
              └─┴─┴─┴───────────┴─┴─┴─┘
```

### Running Tests

```bash
# All tests (offline, no API keys needed)
pytest -q

# Specific tier
pytest tests/unit/ -q
pytest tests/statutory/ -q
pytest tests/contract/ -q
pytest tests/drift/ -q
pytest tests/e2e/ -q

# Verbose with coverage
pytest --cov=hrms_plugin --cov-report=html -v

# Single test file
pytest tests/unit/test_supervisor.py -v
```

### Test Configuration (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
minversion = "7.0"
addopts = "-q --import-mode=importlib"
testpaths = ["tests"]
asyncio_mode = "auto"  # async tests run automatically
```

### Unit Test Coverage (24 files)

| Test File | Module Under Test |
|---|---|
| `test_supervisor.py` | Intent classification, message routing |
| `test_compliance_agent.py` | Labor law violation detection |
| `test_introspector.py` | OpenAPI/Swagger parsing |
| `test_synthesizer.py` | Field matching & FieldMap generation |
| `test_mapping.py` | Path resolution, transforms, bidirectional mapping |
| `test_prober.py` | Shadow verification & quality scoring |
| `test_security.py` | PII masking, RBAC, audit headers |
| `test_resilience.py` | Circuit breaker, rate limiter |
| `test_concurrency.py` | Idempotency, optimistic locking |
| `test_agentic_llm.py` | LLM gateway, provider routing |
| `test_auth_manager.py` | OAuth2 token rotation |
| `test_dynamic_adaptation.py` | End-to-end schema adaptation |
| `test_storage.py` | SQLite, mapping, vault stores |
| `test_server_routes.py` | FastAPI route testing |
| `test_recruitment_agent.py` | ATS scoring, bias detection |
| `test_leave_attendance_agent.py` | Leave evaluation, travel anomaly |
| `test_onboarding_agent.py` | Offer eval, KYC, provisioning |
| `test_offboarding_agent.py` | FNF, timeline audit, clearance |
| `test_travel_expense_agent.py` | Expense auditing |
| `test_voice_agent.py` | Sarvam AI integration |
| `test_rag.py` | Statutory KB search |
| `test_cli_and_mcp_extended.py` | CLI commands, MCP tools |
| `test_vendor_profiles_extended.py` | Vendor profile adapters |
| `test_zoho_profile.py` | Zoho People adapter |

---

## 10. Common Development Workflows

### Adding a New Agent

1. Create `hrms_plugin/agents/your_agent.py` with agent class and Pydantic result models
2. Register in `hrms_plugin/agents/__init__.py` (import + `__all__`)
3. Add intent type to `UserIntent` enum in `supervisor.py`
4. Add keyword matching in `SupervisorAgent.classify_intent()`
5. Add routing case in `SupervisorAgent.process_message()`
6. Register tools in `hrms_plugin/agents/tools.py` (tool definition + execute handler)
7. Add MCP tool in `hrms_plugin/server/mcp_server.py`
8. Write unit test in `tests/unit/test_your_agent.py`

### Adding a New Vendor Profile

1. Create `hrms_plugin/connectors/profiles/your_vendor.py`
2. Extend `VendorProfile` base class
3. Implement: `endpoint_for()`, `prepare_headers()`, `unwrap_response()`, `normalize_payload_for_host()`
4. Register in `hrms_plugin/connectors/profiles/__init__.py`
5. Write unit test in `tests/unit/`

### Adding a New Canonical Entity

1. Add entity dataclass to `hrms_plugin/schema/canonical.py`
2. Add to `EntityType` enum
3. Add to `CanonicalEntity` union type
4. Add `CANONICAL_SPECIFICATIONS` entry in `synthesizer.py` (synonyms, transforms)
5. Add to `_ENTITY_CLASS_MAP` in `dynamic_rest.py`
6. Write tests

### Adding a New Jurisdiction

1. Add `Jurisdiction` enum value in `hrms_plugin/rag/store.py`
2. Add `LegalCitation` entries to `STATUTORY_CORPUS`
3. Add compliance rules in `compliance.py`
4. Add payroll calculations if applicable to `statutory_payroll.py`
5. Write statutory test in `tests/statutory/`

---

## 11. Gotchas & Known Limitations

| # | Gotcha | Impact |
|---|---|---|
| 1 | **`.env` is committed** | Contains API keys — must be moved to secrets manager before production deployment |
| 2 | **CORS is `allow_origins=["*"]`** | Must be restricted for production |
| 3 | **In-memory idempotency** | `IdempotencyManager` is process-local; resets on restart. Use Redis for multi-instance |
| 4 | **SQLite storage** | Single-writer limitation; swap to PostgreSQL for horizontal scaling |
| 5 | **Fake LLM provider** | Returns deterministic stubs, not real LLM output. Set `LLM_PROVIDER` for actual reasoning |
| 6 | **Voice requires `SARVAM_API_KEY`** | Voice endpoints fail without this key; no fallback provider |
| 7 | **Statutory corpus is static** | Legal citations are hardcoded; needs gazette update process |
| 8 | **Rate limiter is per-process** | Not shared across multiple instances |
| 9 | **Schema drift patches are in-memory** | `DriftEvent.suggested_patch` is not persisted automatically |
| 10 | **Healthcheck path inconsistency** | Dockerfile uses `/health`, app exposes `/healthz` — needs alignment |

---

## 12. Onboarding Checklist

### Day 1: Setup & Orientation

- [ ] Clone the repository and read this KT document
- [ ] Set up Python 3.11+ virtual environment
- [ ] Install with `pip install -e ".[dev]"`
- [ ] Run `pytest -q` — all tests should pass with zero API keys
- [ ] Run `uvicorn hrms_plugin.server.app:app --reload` and hit `/healthz`
- [ ] Read `docs/PHASE_WISE_PLAN.md` for architectural context

### Day 2: Core Concepts

- [ ] Trace the chat flow: `app.py` → `supervisor.py` → specialist agent → response
- [ ] Understand canonical models in `schema/canonical.py`
- [ ] Run the CLI: `python -m hrms_plugin.cli calculate-eosb --basic 15000 --tenure 3.5`
- [ ] Read the schema engine pipeline: introspector → synthesizer → mapping → prober → drift

### Day 3: Deep Dive

- [ ] Read `supervisor.py` end-to-end — understand intent classification and routing
- [ ] Understand `orchestrator.py` ReAct loop
- [ ] Read `dynamic_rest.py` — understand how the connector works with mappings and profiles
- [ ] Study the security layer: PII masking flow, RBAC matrix, audit headers

### Day 4: Extension

- [ ] Write a small test extending an existing agent
- [ ] Try adding a new CLI command
- [ ] Run the Docker deployment and verify both API and SDK containers
- [ ] Review a vendor profile (e.g., `iceipts.py`) to understand host adaptation

---

## 13. Glossary

| Term | Definition |
|---|---|
| **Canonical Model** | Vendor-neutral domain dataclass (e.g., `CanonicalEmployee`) that agents reason against |
| **FieldMap** | Declarative mapping from one or more vendor fields to one canonical field with type transforms |
| **EntityMapping** | Complete set of FieldMaps for one entity type from one vendor |
| **VendorProfile** | Adapter that encapsulates endpoint paths, response unwrapping, and error extraction for a specific host HRMS |
| **Shadow Probing** | Offline dry-run of candidate mappings against sample records to verify quality |
| **Schema Drift** | Runtime detection of unmapped fields or changed host schemas |
| **ReAct** | Reasoning + Acting: LLM thinks → calls tools → observes results → iterates |
| **HITL** | Human-In-The-Loop: Human approval gate before executing irreversible mutations |
| **EOSB** | End of Service Benefits (UAE gratuity under Article 51) |
| **FNF** | Full and Final Settlement (terminal payout on employee exit) |
| **WPS** | Wages Protection System (UAE mandatory wage transfer channel) |
| **PII Vault** | Ephemeral in-memory + persistent store of tokenized PII with tenant isolation |
| **CTC** | Cost to Company (India compensation metric) |
| **MCP** | Model Context Protocol — standard interface for AI assistants to discover and call tools |
| **SSE** | Server-Sent Events — HTTP-based real-time streaming from server to client |

---

> **Questions?** Reach out to the codebase owner or refer to `docs/PHASE_WISE_PLAN.md` for the full engineering roadmap.
