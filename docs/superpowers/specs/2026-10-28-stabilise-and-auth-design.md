# Design: Stabilise the Build (A) and Authenticate Every Request (B)

* **Date**: 2026-10-28
* **Status**: Awaiting user review
* **Roadmap**: Phase 7 in `docs/PHASE_WISE_PLAN.md` §10
* **Order of pieces**: A+B (this spec) → C+D → E, F, G, H
* **Source of findings**: audit in `docs/PHASE_WISE_PLAN.md` §8

---

## 1. Goal

Make the service build, boot and pass CI. Then make sure every request (except `/healthz`) proves who the caller is, which tenant they belong to and what they may do, using tokens the host HRMS signs.

## 2. Non-negotiable engineering rules

### 2.1 Test-driven development (applies to every change in this spec)

1. **Red**: write the test first and run it. It must fail **for the expected reason**, and that failure output is recorded in the task log. A test that passes before the change is rewritten until it fails.
2. **Green**: write the minimum code that makes it pass. Run the full suite, not just the new test.
3. **Refactor**: tidy up with all tests green, then re-run the full suite.
4. Each behaviour has **positive, negative and boundary** tests, listed in §5 and §6. A behaviour missing any of the three is not done.
5. A bug fix starts with a test that reproduces the bug.
6. No production code is written without a failing test that requires it. Config-only changes (lint fixes, YAML) are covered by the acceptance commands in §8.

### 2.2 No assumptions

Any behaviour not specified here is decided by asking the user, not by the implementer. When the code contradicts this spec, the implementer stops and reports.

## 3. Decisions log

| # | Decision | Chosen by |
|---|---|---|
| D1 | Host signs tokens with a private key (RS256 / ES256); the plugin verifies with public keys from a JWKS URL | User |
| D2 | One plugin deployment trusts many issuers through a per-tenant registry | User |
| D3 | The registry is a YAML file read and validated at startup; a reload requires a restart | User |
| D4 | Host role strings map to plugin roles through a per-tenant table; unmapped roles grant nothing | User |
| D5 | All HTTP routes except `/healthz` require a token and a declared permission. The MCP server has no transport and is out of scope; any future MCP transport must reuse this auth module | Architect (delegated by user) |
| D6 | Enforcement is a FastAPI dependency on each route, plus a test that fails if a route lacks one | Architect, accepted |
| D7 | New intent `RECRUITMENT_REQUISITION` | User |
| D8 | `MANAGE_SCHEMA` granted to SUPER_ADMIN only | User |
| D9 | New permission `APPROVE_EXPENSE`, granted to HR_ADMIN, PAYROLL_ADMIN, DEPARTMENT_MANAGER and SUPER_ADMIN | User |
| D10 | Tool permission table exactly as in §6.5 | User |
| D11 | `EXECUTE_REMEDIATION` requires a token issued ≤ 300 s ago | User |

## 4. Root causes behind piece A (verified)

| Symptom | Root cause | Evidence |
|---|---|---|
| Container cannot start | `python -m hrms_plugin.server` needs `hrms_plugin/server/__main__.py`, which does not exist | Running it prints "No module named hrms_plugin.server.__main__" |
| Compose healthcheck and k8s probes fail | They call `/health`; the route is `/healthz` | `deploy/docker-compose.yml`, `deploy/k8s/deployment.yaml:47,53` |
| `test_chat_endpoint_payroll_routing` fails | The orchestrator always returns `intent=GENERAL_HR` | `agents/orchestrator.py:124,196` |
| `test_e2e_compliance_audit_and_remediation_lifecycle` fails | Tool arguments come from the LLM. The fake LLM picks the jurisdiction from message keywords and ignores the request's `"AE"`; `context.employees` never reaches the tool | `ai/llm.py` FakeLLMClient; `agents/tools.py:391-409` |
| CI is red | 11 ruff errors (E501 ×9, F541 ×2) | `agents/tools.py` |
| Container runs as root and writes to its working directory | No `USER` in the Dockerfile; DB path `.hrms_data/` is relative | `deploy/Dockerfile`, `server/app.py:68` |
| A broken container passed CI | CI never builds or boots the image | `.github/workflows/ci.yml` |
| A missing jurisdiction is guessed silently | 7 tools default `jurisdiction`, inconsistently (`"IN"` or `"AE"`) | `agents/tools.py:369-529` |

## 5. Piece A — Stabilise

### A1. Server entrypoint and runtime user

**Change**:
- Add `hrms_plugin/server/__main__.py`, which runs `uvicorn.run(app, host=HOST, port=PORT)`.
  - `HOST` defaults to `127.0.0.1`. The Dockerfile sets `HOST=0.0.0.0`.
  - `PORT` defaults to `8000` and must be an integer from 1 to 65535.
- Add `HRMS_DATA_DIR`, default `./.hrms_data`. The SQLite path becomes `<HRMS_DATA_DIR>/plugin_storage.db`.
  - At startup the directory is created with mode 0700 if missing. If it is not writable, startup exits non-zero.
- The Dockerfile adds a non-root user, sets `HRMS_DATA_DIR` to a directory owned by that user, and switches `USER` before `CMD`.

**Tests** (`uvicorn.run` is mocked):

| Type | Case | Expected |
|---|---|---|
| Positive | No env vars | `run(host="127.0.0.1", port=8000)` |
| Positive | `HOST=0.0.0.0 PORT=9000` | Passed through unchanged |
| Boundary | `PORT=1`, `PORT=65535` | Accepted |
| Negative | `PORT=0`, `65536`, `-1`, `abc`, `""`, `8000.5` | Exit code ≠ 0, a message naming `PORT`, `run` not called |
| Boundary | `HRMS_DATA_DIR` missing but creatable | Created, mode 0700 |
| Negative | `HRMS_DATA_DIR` exists but is read-only | Exit code ≠ 0, a message naming the path, `run` not called |
| Negative | `HRMS_DATA_DIR` points to a file | Exit code ≠ 0 |

### A2. Deploy paths match real routes; CI boots the container

**Change**:
- The compose healthcheck and both k8s probes use `/healthz`.
- New CI job `container-smoke`: build the image, run it, poll `/healthz` for up to 30 s, and fail unless it returns HTTP 200 with `{"status": "healthy"}`.
  - The job supplies the registry fixture from §6.1, because the app refuses to start without one.

**Tests**:

| Type | Case | Expected |
|---|---|---|
| Positive | Parse `docker-compose.yml` and `k8s/deployment.yaml`; collect every probe/healthcheck path | Each is a registered GET route on `app` |
| Negative | Fixture YAML with path `/health` | Test reports the unknown path |
| Boundary | Probe path with query string `/healthz?x=1` | Path part matched; accepted |

### A3. Lint

**Change**: fix the 11 ruff errors. No rule suppressions.
**Acceptance**: `ruff check .` exits 0.

### A4. Intent comes from the tools that ran

**Change**:
- Add `UserIntent.RECRUITMENT_REQUISITION`.
- `TOOL_INTENT` maps each of the 14 tools to one intent:

| Tool | Intent |
|---|---|
| `calculate_salary_structure` | `PAYROLL_STRUCTURE` |
| `calculate_eosb_gratuity` | `PAYROLL_EOSB` |
| `audit_statutory_compliance` | `COMPLIANCE_AUDIT` |
| `search_labor_regulations` | `LEGAL_QNA` |
| `evaluate_leave_request` | `LEAVE_APPLY` |
| `detect_travel_anomaly` | `ATTENDANCE_ANOMALY` |
| `score_candidate_ats` | `RECRUITMENT_ATS` |
| `audit_job_description_bias` | `RECRUITMENT_BIAS` |
| `evaluate_onboarding_offer` | `ONBOARDING` |
| `verify_onboarding_documents` | `ONBOARDING` |
| `audit_offboarding_fnf_timeline` | `OFFBOARDING_FNF` |
| `compute_final_settlement` | `OFFBOARDING_FNF` |
| `audit_travel_expense_claim` | `TRAVEL_EXPENSE` |
| `generate_requisition_archetypes` | `RECRUITMENT_REQUISITION` |

- The response intent is the intent of the **first tool executed**. With no tool executed, or a tool missing from the table, it is `GENERAL_HR`.
- The rule lives in **one helper**, used by both `run()` and `run_stream()`. The stream reports the intent in its final event.

**Tests** (scripted LLM, see A6):

| Type | Case | Expected |
|---|---|---|
| Positive | Parametrised over all 14 tools | The mapped intent |
| Positive | Every registered tool has a `TOOL_INTENT` entry | Fails if a tool is added without one |
| Boundary | Zero tool calls | `GENERAL_HR` |
| Boundary | Two different tools in one run | Intent of the first executed |
| Boundary | Step limit reached | Intent still set from the first tool |
| Negative | LLM names an unknown tool | Not executed; `GENERAL_HR` if nothing else ran |
| Parity | Same script through `run()` and `run_stream()` | Same intent |

### A5. Trusted values override LLM arguments

**Change**:
- `jurisdiction` becomes a **required** parameter (no default) on the 7 tools that take it.
- The orchestrator injects `jurisdiction` (from the request) and `tenant_id` (from the request now, from the token after B) into every tool whose schema declares the parameter, **replacing** any LLM-supplied value.
- Context records are injected only into tools that declare a parameter of the same name. In this piece that means `employees` for `audit_statutory_compliance`, overriding any LLM-supplied value.
- Each override is recorded in the step trace as `{"param", "llm_value", "trusted_value"}`.
- Implemented in one helper used by `run()` and `run_stream()`.
- `ChatRequest.jurisdiction` accepts only `IN`, `AE`, `SA` or `US`, exact case.

**Out of scope, stated explicitly**:
- Monetary arguments (for example `gross_or_ctc`) still come from the LLM. "No LLM-supplied numbers" is delivered in pieces F/G.
- US and Saudi payroll still fall back to India rules; this is a known gap tracked for a later piece.

**Tests**:

| Type | Case | Expected |
|---|---|---|
| Positive | LLM omits `jurisdiction`; request `AE` | Tool receives `AE` |
| Negative | LLM sends `IN`; request `AE` | Tool receives `AE`; override recorded in the trace |
| Positive | LLM omits `employees`; request context carries 1 employee | Tool receives that employee |
| Negative | LLM sends its own `employees`; context carries others | Context value used; override recorded |
| Boundary | Context has no `employees` key | Nothing injected; the tool's own handling applies |
| Boundary | Tool without a `jurisdiction` parameter (e.g. `audit_job_description_bias`) | No extra argument passed |
| Negative | A tool is called directly without `jurisdiction` | `TypeError`; no default guessed |
| Negative | API `jurisdiction` is `ae`, `UAE`, `""` or `XX` | 422 |
| Parity | Same script through `run()` and `run_stream()` | Same injected arguments |
| Regression | The two originally failing tests, rewritten with the scripted LLM | Pass |

### A6. Deterministic LLM double for tests

**Change**: add `tests/support/scripted_llm.py`. `ScriptedLLMClient` takes a list of `LLMResponse` objects and returns them in order.
- One call more than scripted raises `AssertionError`.
- Fewer calls than scripted fails the test on teardown.

The production `FakeLLMClient` is unchanged; piece E decides its future.

**Tests**:
- **Positive:** the client returns each scripted response in order.
- **Negative:** an extra call raises; leftover scripted responses fail on teardown.
- **Boundary:** an empty script fails on the first call.

## 6. Piece B — Authenticate and authorise

### 6.1 Tenant registry (`hrms_plugin/security/tenant_registry.py`)

Loaded from the YAML at `HRMS_TENANT_REGISTRY`. Startup fails, naming the problem, when the variable is unset, the file is missing or unreadable, or validation fails. Once loaded the registry is immutable.

```yaml
tenants:
  - tenant_id: acme
    issuer: https://hr.acme.com
    jwks_url: https://hr.acme.com/.well-known/jwks.json
    audience: hrms-agentic-plugin
    algorithms: [RS256]
    claims: {tenant: tid, roles: roles, subject: sub}
    role_map: {hr_manager: HR_ADMIN, payroll_officer: PAYROLL_ADMIN}
    allowed_origins: [https://hr.acme.com]
    leeway_seconds: 30
    max_token_lifetime_seconds: 3600
```

| Field | Rule |
|---|---|
| (any) | Unknown keys are rejected |
| `tenants` | Non-empty list |
| `tenant_id` | Matches `^[A-Za-z0-9_-]{1,64}$`; unique across the file |
| `issuer` | Non-empty string, compared exactly against `iss` |
| `jwks_url` | Absolute `https://` URL |
| `audience` | Non-empty string |
| `algorithms` | Non-empty subset of `{RS256, ES256}` |
| `claims.tenant`, `claims.roles`, `claims.subject` | Non-empty strings. Tenants that share an `issuer` must have identical `claims` |
| `role_map` | Non-empty; every value is a valid `UserRole` |
| `allowed_origins` | List of exact origins: `https` scheme, host, optional port, no path. `*` is rejected |
| `leeway_seconds` | Integer, 0–120 |
| `max_token_lifetime_seconds` | Integer, 60–86400 |

**Tests**:
- **Positive:** a valid file with one tenant; a valid file with two tenants sharing an issuer.
- **Negative:**
  - Each field rule is violated once, giving one parametrised case per rule.
  - Variable unset; file missing; invalid YAML.
  - Duplicate `tenant_id`.
  - Two tenants share an issuer but have different `claims`.
  - An `http://` JWKS URL.
  - `*` as an origin; an origin with a path.
  - An unknown role in `role_map`.
- **Boundary:** `leeway_seconds` 0 and 120 accepted, −1 and 121 rejected; lifetime 60 and 86400 accepted, 59 and 86401 rejected; `tenant_id` of 64 characters accepted, 65 rejected.

### 6.2 Token verification (`hrms_plugin/security/token_verifier.py`)

Library: PyJWT with the `crypto` extra, version pinned in `pyproject.toml`. Steps run in this order; the first failure ends the request.

1. **Header**:
   - Exactly one `Authorization` header with scheme `Bearer`, compared case-insensitively.
   - A non-empty token of at most 8192 bytes.
2. **Locate tenant**:
   - Decode *without verification* only to read `iss` and the tenant claim. The tenant claim's name comes from the registry entries for that `iss`.
   - No matching registry entry → 401.
3. **Header checks**:
   - `alg` must be in the entry's `algorithms`.
   - `kid` is required.
   - `jku`, `x5u` and `jwk` header parameters are ignored; nothing is ever fetched from a token-supplied URL.
4. **Signature and registered claims**:
   - Verify the signature with the key matching `kid` from the entry's JWKS (§6.3).
   - `iss` must equal the entry's `issuer`, and `aud` must contain the entry's `audience`.
   - `exp` and `iat` are required; `nbf` is checked if present. All use `leeway_seconds`.
   - `exp − iat ≤ max_token_lifetime_seconds`.
   - `iat ≤ now + leeway_seconds`.
5. **Identity claims**:
   - The subject claim is a non-empty string of at most 256 characters.
   - The tenant claim equals the entry's `tenant_id`, case-sensitive.
   - The roles claim is a JSON list of at most 50 strings. Any other shape → 401.
6. **Roles**:
   - Each role string is looked up in `role_map`; unmapped strings are ignored and logged at INFO with their name.
   - The result is `AuthContext(user_id, tenant_id, roles: frozenset[UserRole], issued_at, token_id)`, where `token_id` is the `jti` if present.

`AuthContext.role` (single) is replaced by `roles` (set). `RbacPolicyEnforcer.is_authorized` returns True if **any** role grants the permission. `validate_separation_of_duties` keeps its current rules, now using `user_id` from the token.

**Tests** use session fixtures that generate RSA-2048, RSA-1024 and EC P-256 keys and a minting helper that can set any header or claim:

| Type | Case | Expected |
|---|---|---|
| Positive | Valid RS256 token | `AuthContext` with mapped roles |
| Positive | Valid ES256 token | Same |
| Positive | Scheme `bearer` (lowercase) | Accepted |
| Positive | Two tenants share an issuer; each token resolves to its own tenant | Correct tenant for each |
| Negative | No `Authorization` header; `Basic` scheme; empty token; two `Authorization` headers | 401 |
| Negative | `alg=none`; `alg=HS256` signed with the public key as the secret | 401 |
| Negative | `alg=ES256` when the entry allows only RS256 | 401 |
| Negative | Missing `kid`; `kid="../x"` | 401 |
| Negative | `jku` set to an attacker URL | Ignored: no fetch occurs, and verification uses the registry JWKS |
| Negative | Bad signature; wrong `iss`; wrong `aud` | 401 |
| Negative | Unknown issuer; known issuer but unregistered tenant | 401 |
| Negative | Token tenant B with tenant A's issuer, where B is registered under another issuer | 401 |
| Negative | Tenant claim `ACME` versus registry `acme` | 401 |
| Negative | Missing `exp`, `iat` or `sub`; empty `sub`; 257-character `sub` | 401 |
| Negative | Roles claim is a string, or contains a non-string | 401 |
| Boundary | Token of 8192 bytes accepted; 8193 rejected | — |
| Boundary | `exp = now − leeway` accepted; `now − leeway − 1` rejected | — |
| Boundary | `nbf = now + leeway` accepted; `now + leeway + 1` rejected | — |
| Boundary | `iat = now + leeway` accepted; `now + leeway + 1` rejected | — |
| Boundary | Lifetime exactly max accepted; max + 1 rejected | — |
| Boundary | 50 roles accepted; 51 rejected | — |
| Boundary | 256-character `sub` accepted | — |
| Boundary | Only unmapped roles | Authenticated with an empty role set; protected routes return 403 |
| Positive | Mixed mapped and unmapped roles | Union of mapped roles; unmapped ones logged |

Time is injected (a clock function), so boundary tests are exact and not flaky.

### 6.3 JWKS cache (`hrms_plugin/security/jwks_cache.py`)

- **Fetcher**: an injectable async function. Production uses one shared `httpx.AsyncClient` with a 5 s timeout.
  - A response over 64 KB, a non-JSON body, or a non-200 status counts as a failed fetch.
- **Accepted keys**: RSA with modulus ≥ 2048 bits, or EC on curve P-256, with `use` absent or `"sig"`. Other keys are skipped and logged.
- **Freshness**: keys are cached per tenant for 600 s.
- **Unknown `kid`**: triggers one refresh, rate-limited to one per tenant per 60 s. Concurrent requests share one in-flight fetch.
- **Failure policy**:
  - If a fetch fails and the cached keys are ≤ 3600 s old, use the cache.
  - If the cache is older or empty, return 503 with body `{"error": "AUTH_UNAVAILABLE"}`.

**Tests**:

| Type | Case | Expected |
|---|---|---|
| Positive | First request fetches; the second within 600 s does not | One fetch |
| Positive | Key rotation: new `kid` appears after a refresh | Accepted |
| Negative | Response over 64 KB; non-JSON; HTTP 500; timeout, all with an empty cache | 503 |
| Negative | JWKS contains only an RSA-1024 key | 401 (no usable key) |
| Negative | `use: "enc"` key with matching `kid` | 401 |
| Boundary | Cache age 600 s triggers a refresh, 599 s does not | — |
| Boundary | Fetch fails with cache 3600 s old: cache used. At 3601 s: 503 | — |
| Boundary | Unknown `kid` twice within 60 s: one refresh. At 60 s: second refresh allowed | — |
| Concurrency | 100 concurrent requests with an unknown `kid` | Exactly one fetch |

### 6.4 Route permissions

New permissions: `USE_COPILOT`, `MANAGE_SCHEMA`, `APPROVE_EXPENSE`.

| Role | Added permissions |
|---|---|
| SUPER_ADMIN | All (unchanged rule: every permission) |
| HR_ADMIN | `USE_COPILOT`, `APPROVE_EXPENSE` |
| PAYROLL_ADMIN | `USE_COPILOT`, `APPROVE_EXPENSE` |
| HIRING_MANAGER | `USE_COPILOT` |
| DEPARTMENT_MANAGER | `USE_COPILOT`, `APPROVE_EXPENSE` |
| EMPLOYEE | `USE_COPILOT` |

`MANAGE_SCHEMA` is therefore held only by SUPER_ADMIN.

Each route declares `Depends(require(Permission.X))`:

| Route(s) | Permission |
|---|---|
| `POST /v1/chat`, `POST /v1/chat/stream`, `POST /api/compliance/agent/chat` | `USE_COPILOT` |
| `POST /v1/voice/transcribe`, `/v1/voice/synthesize`, `/v1/voice/turn` | `USE_COPILOT` |
| `POST /v1/schema/introspect`, `/v1/schema/synthesize`, `/v1/schema/synthesize/agentic` | `MANAGE_SCHEMA` |
| `GET /v1/schema/mappings`, `/v1/schema/mappings/{vendor}/{entity_type}` | `MANAGE_SCHEMA` |
| `POST /v1/remediate/execute` | `EXECUTE_REMEDIATION`, plus the freshness rule in §6.7 |
| `GET /v1/compliance/diagnose`, `/api/compliance/agent/diagnose` | `VIEW_COMPLIANCE` |
| `GET /v1/compliance/dashboard/live`, `/api/compliance/dashboard/live` | `VIEW_COMPLIANCE` |
| `POST /api/requisitions/generate-archetypes`, `/v1/requisitions/generate-archetypes` | `MANAGE_RECRUITMENT` |
| `GET /healthz` | Public |

**Tests**:
- **Coverage:** enumerate `app.routes`; every route except `/healthz` has a `require` dependency, and its permission equals this table. A route added later without one fails the test.
- **Per route, parametrised:** no token → 401; valid token lacking the permission → 403; valid token with the permission → not 401/403.
- **Boundary:** a user holding two roles where only one grants the permission is allowed.
- **Public route:** `/healthz` with no token → 200.

### 6.5 Tool permissions

| Tool | Permission |
|---|---|
| `calculate_salary_structure`, `calculate_eosb_gratuity`, `audit_offboarding_fnf_timeline`, `compute_final_settlement` | `VIEW_SALARY` |
| `audit_statutory_compliance` | `VIEW_COMPLIANCE` |
| `evaluate_leave_request` | `APPLY_LEAVE` |
| `detect_travel_anomaly` | `APPROVE_LEAVE` |
| `score_candidate_ats`, `audit_job_description_bias`, `evaluate_onboarding_offer`, `generate_requisition_archetypes` | `MANAGE_RECRUITMENT` |
| `verify_onboarding_documents` | `VIEW_EMPLOYEE` |
| `search_labor_regulations` | `USE_COPILOT` |
| `audit_travel_expense_claim` | `APPROVE_EXPENSE` |

Consequence (accepted): EMPLOYEE has no `VIEW_SALARY`, so employees cannot use the salary or EOSB calculators.

**Change**:
- Each tool registration declares its permission, and registration without one raises an error.
- The orchestrator passes the LLM only the tools the caller may use.
- At execution time it re-checks. A call to a tool that is not permitted is not run; the LLM receives `{"error": "TOOL_NOT_PERMITTED"}`, and the attempt is logged with `sub`, tenant and tool name.
- Applies to `run()` and `run_stream()` through the shared helper.

**Tests**:
- **Positive:** an HR_ADMIN's offered tools equal exactly the set derived from this table.
- **Negative:** an EMPLOYEE is not offered `audit_statutory_compliance`. A scripted LLM calls it anyway; it is not run, `TOOL_NOT_PERMITTED` is returned, and a log record exists.
- **Boundary:** a user with zero roles is offered no tools, and the LLM call is made without a `tools` parameter.
- **Coverage:** every registered tool has a permission; registration without one raises.
- **Parity:** `run()` and `run_stream()` filter and re-check identically.

### 6.5a Same permissions on the non-agentic path

`POST /v1/chat` with `agentic: false`, and `POST /api/compliance/agent/chat`, call `SupervisorAgent.process_message`. That method invokes domain agents directly by keyword (`supervisor.py:194-531`) and bypasses the tool layer.

**Change**:
- Each branch of `process_message` checks the permission of its equivalent tool in §6.5 **before** calling the agent. Branches that perform two actions check per action:
  - `ONBOARDING`: document verification needs `VIEW_EMPLOYEE`; offer evaluation needs `MANAGE_RECRUITMENT`.
  - `OFFBOARDING_FNF`: timeline and settlement both need `VIEW_SALARY`.
- A denied check returns **403** `{"error": "FORBIDDEN", "permission": "<name>"}`. No partial result is returned.
- `process_message` takes `AuthContext` as a required argument; calling it without one raises an error.

**Tests**:
- **Negative:** an EMPLOYEE token with `agentic: false` asking for a final settlement → 403 `VIEW_SALARY`. The same through the legacy adapter → 403.
- **Positive:** an HR_ADMIN token, same request → 200.
- **Boundary:** an EMPLOYEE onboarding message about documents → 200 (has `VIEW_EMPLOYEE`); the same user's onboarding message about an offer → 403 `MANAGE_RECRUITMENT`.
- **Coverage:** every `UserIntent` branch in `process_message` has a permission check, verified by a parametrised test over all intents.

### 6.6 Tenant header and request bodies

- **`X-Tenant-Id`**:
  - Absent: allowed.
  - Equal to the token's tenant: allowed.
  - Different: **403** `{"error": "TENANT_MISMATCH"}`.
- **All handlers** take `tenant_id` from `AuthContext` only.
- **Remediation request**: `RemediationExecuteRequest` removes `approver_id` and `approver_role` and sets `extra="forbid"`.
  - The approver is `AuthContext.user_id`, and it is also used as `hitl_approver_id` in the audit headers.
- **Copilot SDK**: it sends the removed fields and no token, so it stops working until piece D. This is accepted.

**Tests**:
- **Header:** header absent, equal and different (403).
- **Remediation:** a body containing `approver_role` gets 422. The audit header `X-HITL-Approver-Id` equals the token's `sub`.
- **Tenant source:** the handlers' tenant comes from the token even when a matching header is present.

### 6.7 Recent authentication for remediation

`POST /v1/remediate/execute` additionally requires `now − iat ≤ 300` seconds. This check uses no leeway. Otherwise it returns **401** `{"error": "REAUTH_REQUIRED"}`.

**Tests**:
- **Boundary:** `iat` exactly 300 s old is accepted; 301 s is rejected with `REAUTH_REQUIRED`.
- **Positive:** a freshly minted token is accepted.
- **Scope:** other routes accept a 301 s-old token that is otherwise valid.

### 6.8 CORS

- `allow_origins` is the union of all tenants' `allowed_origins`.
- `allow_credentials=False`.
- Methods: `GET`, `POST`, `OPTIONS`. Headers: `Authorization`, `Content-Type`, `X-Tenant-Id`.

**Tests**:
- **Allowed origin:** a preflight returns `Access-Control-Allow-Origin` equal to the origin.
- **Disallowed origin:** no such header.
- **Credentials:** `Access-Control-Allow-Credentials` is never `true`.
- **Variants:** an origin differing only by port or scheme is disallowed.

### 6.9 Errors, logging, streaming

- **401**:
  - Body `{"error": "UNAUTHENTICATED"}` (or `REAUTH_REQUIRED`, §6.7).
  - Header `WWW-Authenticate: Bearer error="invalid_token"`.
  - The response never says which check failed.
- **403**: body `{"error": "FORBIDDEN", "permission": "<name>"}`, or `TENANT_MISMATCH`.
- **503**: body `{"error": "AUTH_UNAVAILABLE"}`.
- **Logging**: every rejection is logged with a reason code (for example `ALG_NOT_ALLOWED` or `EXPIRED`), `iss`, tenant, `sub` and `jti` when known. **The raw token is never logged.**
- **Streaming**: authentication completes before `StreamingResponse` is created, so failures are ordinary HTTP errors. A token that expires during a stream does not interrupt it; the orchestrator's step limit bounds the stream.

**Tests**:
- **Body:** the 401 body is identical for `EXPIRED` and `BAD_SIGNATURE`, while the logged reason codes differ.
- **Log content:** no log record contains the token string, checked by capturing logs and searching for it.
- **Streaming:** a stream request with no token gets HTTP 401 with no event-stream body.

### 6.10 Accepted risks

- **Replay:** `jti` values are not tracked, so a stolen token can be replayed until it expires. The lifetime cap (§6.1) and the 300 s freshness rule for remediation (§6.7) limit this.
- **Registry changes:** a change needs a restart (D3).

## 7. Requirements carried to later pieces

| Piece | Requirement found during this design |
|---|---|
| C | Vault lookups must take the tenant from `AuthContext` |
| D | The SDK must obtain a token from the host page, send `Authorization`, and stop sending approver fields |
| E | Decide `FakeLLMClient`'s fate: it is silently used in production when no key is set |
| F | Remediation patches must be rebuilt server-side from host data, never taken from the request (today `context.employees` is client-supplied). Tools take record IDs, not amounts |
| F | Any MCP transport must use this auth module |
| Later | US and Saudi payroll are not implemented; they currently fall back to India rules |

## 8. Acceptance (piece is done only when every command exits 0)

```bash
ruff check .
python3.11 -m pytest -q
python3.12 -m pytest -q
# CI job container-smoke: image builds, runs as non-root, /healthz returns 200 within 30 s
```

Plus:
- **Red→green evidence:** every test in §5–§6 has its red→green record in the task log.
- **Hostile smoke test:** against the running container, `POST /v1/remediate/execute` without a token returns 401, and with an EMPLOYEE token returns 403.
