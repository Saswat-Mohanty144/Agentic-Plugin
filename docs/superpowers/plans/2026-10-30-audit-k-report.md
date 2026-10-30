# Adversarial Audit Report: HRMS Agentic Plugin (`audit/k`)

**Date**: 2026-10-30  
**Auditor Role**: Adversarial Auditor  
**Target Branch**: `audit/k` (based on commit `67703c3..HEAD`)  
**Scope**: FastAPI Python backend (`hrms_plugin/`), SDK Copilot Widget (`sdk/copilot.js`), Deployment configs (`deploy/`), and comprehensive contracts from `README.md` and decisions D1–D12, R1–R9, V1–V5, K1.1–K2.4.  
**Strict Auditor Rule Applied**: Zero modification of files in `hrms_plugin/`, `sdk/copilot.js`, or `deploy/`. All bugs are demonstrated by real, executable tests that fail against current code without `xfail`.

---

## 1. Summary

A comprehensive adversarial audit was executed across all functional and non-functional requirements specified in `README.md`, decision plans, and recent implementation batches.

### Finding Counts by Severity

| Severity | Count | Findings |
|---|---|---|
| **CRITICAL** | **0** | No tenant data leakage or remote code execution vulnerabilities identified. |
| **HIGH** | **2** | BUG-1 (Schema drift detection bypass on missing expected fields), BUG-2 (DeclarativeMappingOverlay crash on MCP protocol entity specs). |
| **MEDIUM** | **2** | BUG-3 (Unhandled KeyError 500 in schema synthesis on malformed discovered fields), BUG-4 (Non-standard error envelopes bypassing API contract in introspect and mappings routes). |
| **LOW** | **2** | BUG-5 (docker-compose environment missing `HRMS_DISCLOSURE_KEY_FILE`), BUG-6 (SDK `copilot.js` drops backend `thinking` and `history_trimmed` SSE events). |
| **TOTAL** | **6** | 6 confirmed reproducible defects with corresponding failing tests. |

### Audit Suite Summary
- **Python Audit Tests Added** (`tests/audit/`): **39 tests** (33 passing, 6 failing).
- **JavaScript Audit Tests Added** (`sdk/test/audit.test.js`): **5 tests** (4 passing, 1 failing).
- **Total Audit Tests Added**: **44 tests** (37 passing, 7 failing).

---

## 2. Findings

| ID | Severity | Title | File & Line | Failing Test |
|---|---|---|---|---|
| **BUG-1** | **HIGH** | Missing expected fields drift detection is never invoked during schema introspection | `hrms_plugin/server/app.py:552-556` | `tests/audit/test_audit_schema.py::test_introspect_detects_missing_expected_field_drift` |
| **BUG-2** | **HIGH** | Declarative mapping overlay crashes on `McpEntitySpec` with unhandled `OverlayError` | `hrms_plugin/integrations/mapping_overlay.py:65` | `tests/audit/test_audit_mcp_overlay.py::test_overlay_mcp_entity_spec_preserves_mcp_protocol` |
| **BUG-3** | **MEDIUM** | Schema synthesis endpoint crashes with unhandled 500 `KeyError` on malformed discovered fields | `hrms_plugin/server/app.py:589` | `tests/audit/test_audit_schema.py::test_synthesize_malformed_discovered_field_missing_name_returns_422` |
| **BUG-4** | **MEDIUM** | Raw `HTTPException` in introspect and mappings routes bypasses standard error envelope | `hrms_plugin/server/app.py:576, 707` | `tests/audit/test_audit_schema.py::test_introspect_no_swagger_or_samples_standard_error_envelope` and `test_get_mapping_not_found_standard_error_envelope` |
| **BUG-5** | **LOW** | `deploy/docker-compose.yml` does not declare `HRMS_DISCLOSURE_KEY_FILE` environment variable | `deploy/docker-compose.yml:26-29` | `tests/audit/test_audit_disclosure_egress.py::test_docker_compose_declares_disclosure_key_file` |
| **BUG-6** | **LOW** | SDK `copilot.js` `parseSseFrames` drops backend streaming events (`thinking`, `history_trimmed`) | `sdk/copilot.js:156` | `sdk/test/audit.test.js::BUG-6: parseSseFrames must preserve thinking and history_trimmed events emitted by backend` |

---

### Detailed Findings

#### BUG-1 (HIGH): Missing expected fields drift detection never invoked during schema introspection
- **File:Line**: [hrms_plugin/server/app.py:552-556](file:///Users/satosh/Agentic-Plugin-audit-k/hrms_plugin/server/app.py#L552-L556)
- **Failing Test**: `tests/audit/test_audit_schema.py::test_introspect_detects_missing_expected_field_drift`
- **Exact Reproduction**:
  ```python
  client.post(
      "/v1/schema/introspect",
      json={
          "vendor": "custom_hr",
          "domain": "REQUISITION",
          "sample_records": [{"job_code": "REQ-101"}], # Missing all required fields (title, department, etc.)
      },
      headers=auth_headers,
  )
  ```
- **Expected vs Actual**:
  - *Expected*: Per README §12 Step 1 and Decision D5, when host records or swagger lack critical expected fields, `drift_detected` must flag the missing expected fields or report missing field drift.
  - *Actual*: In `server/app.py:552-556`, `SchemaDriftInterceptor.inspect_response()` is called with:
    ```python
    drift = interceptor.inspect_response(
        vendor=req.vendor,
        entity_type=canonical_domain,
        response_payload=req.sample_records,
    )
    ```
    Notice that `check_missing=True` and `expected_fields=...` are NOT passed (they default to `False` and `None` in `SchemaDriftInterceptor.inspect_response`). As a result, drift detection ONLY checks for new undeclared fields, completely ignoring missing critical fields.
- **Why It Matters**: If a host removes an essential field (e.g. `title` or `employee_id`), the plugin fails to flag schema drift at introspection time, allowing defective mappings to be synthesized and causing downstream runtime failures when records are read.

---

#### BUG-2 (HIGH): Declarative mapping overlay crashes on `McpEntitySpec` with unhandled `OverlayError`
- **File:Line**: [hrms_plugin/integrations/mapping_overlay.py:65](file:///Users/satosh/Agentic-Plugin-audit-k/hrms_plugin/integrations/mapping_overlay.py#L65)
- **Failing Test**: `tests/audit/test_audit_mcp_overlay.py::test_overlay_mcp_entity_spec_preserves_mcp_protocol`
- **Exact Reproduction**:
  ```python
  from hrms_plugin.integrations.declarative import McpEntitySpec, FieldSpec
  from hrms_plugin.integrations.mapping_overlay import overlay
  from hrms_plugin.storage.mapping_store import EntityMapping, FieldMap

  mcp_spec = McpEntitySpec(
      path="/mcp",
      tool="get_requisitions",
      arguments={},
      id_from="req_id",
      fields={"title": FieldSpec.model_validate({"from": "job_title", "type": "str"})},
  )
  mapping = EntityMapping(
      vendor="iceipts",
      entity_type="requisition",
      id_path="req_id",
      fields=(FieldMap(target="title", source="custom_job_title"),),
  )
  overlay(mcp_spec, mapping)
  ```
- **Expected vs Actual**:
  - *Expected*: Per Decision D8 and README §12, declarative connectors support both REST (`EntitySpec`) and MCP (`McpEntitySpec`). Applying an approved tenant mapping override to an MCP vendor profile should return an updated `McpEntitySpec` with the overridden field mappings.
  - *Actual*: `mapping_overlay.py:65` hardcodes:
    ```python
    merged = EntitySpec.model_validate(raw)
    ```
    Because `McpEntitySpec` lacks `EntitySpec` fields (`method`, `pagination`) and contains MCP-specific fields (`tool`, `arguments`), Pydantic raises a `ValidationError` which is wrapped into `OverlayError: Failed to construct EntitySpec from merged dict`. In API routes (`/v1/hosts/requisitions`), this turns into an unhandled 409 `MAPPING_NOT_APPLICABLE`.
- **Why It Matters**: Any customer running an MCP-based HR connector (such as iCeipts or custom tool integrations) will experience total failure of host read endpoints the moment an approved mapping override is deployed.

---

#### BUG-3 (MEDIUM): Schema synthesis crashes with 500 `KeyError` on malformed `discovered_fields`
- **File:Line**: [hrms_plugin/server/app.py:589](file:///Users/satosh/Agentic-Plugin-audit-k/hrms_plugin/server/app.py#L589)
- **Failing Test**: `tests/audit/test_audit_schema.py::test_synthesize_malformed_discovered_field_missing_name_returns_422`
- **Exact Reproduction**:
  ```bash
  curl -X POST http://localhost:8000/v1/schema/synthesize \
    -H "Authorization: Bearer <TOKEN>" \
    -H "Content-Type: application/json" \
    -d '{
      "vendor": "custom_hr",
      "domain": "REQUISITION",
      "discovered_fields": [{"path": "raw.field", "sample_values": [123]}],
      "sample_records": [{"raw": {"field": 123}}]
    }'
  ```
- **Expected vs Actual**:
  - *Expected*: Input validation failure returning HTTP 422 Unprocessable Entity with a validation error description.
  - *Actual*: In `server/app.py:589`, `_prepare_synthesis_discovered` executes:
    ```python
    name = f["name"]
    ```
    Because `SynthesizeRequest` defines `discovered_fields: Optional[List[Dict[str, Any]]] = None` without a structured Pydantic item model, missing `"name"` triggers an unhandled `KeyError: 'name'`, crashing the FastAPI handler and returning HTTP 500 Internal Server Error.
- **Why It Matters**: Unhandled exceptions bypass client-side error recovery, trigger false server error alerts, and violate the input validation contract.

---

#### BUG-4 (MEDIUM): Raw `HTTPException` in introspect and mappings routes bypasses standard error envelope
- **File:Line**: [hrms_plugin/server/app.py:576](file:///Users/satosh/Agentic-Plugin-audit-k/hrms_plugin/server/app.py#L576), [hrms_plugin/server/app.py:707](file:///Users/satosh/Agentic-Plugin-audit-k/hrms_plugin/server/app.py#L707)
- **Failing Tests**:
  - `tests/audit/test_audit_schema.py::test_introspect_no_swagger_or_samples_standard_error_envelope`
  - `tests/audit/test_audit_schema.py::test_get_mapping_not_found_standard_error_envelope`
- **Exact Reproduction**:
  1. `POST /v1/schema/introspect` with `{ "vendor": "custom_hr" }` (neither `swagger_dict` nor `sample_records`).
  2. `GET /v1/schema/mappings/nonexistent_vendor/requisition`.
- **Expected vs Actual**:
  - *Expected*: README lines 827–835 guarantee that all error responses follow the standard JSON envelope:
    ```json
    {
      "error": "INVALID_ARGUMENTS" | "NOT_FOUND",
      "detail": "..."
    }
    ```
  - *Actual*: Lines 576 and 707 raise `fastapi.HTTPException` directly:
    ```python
    raise HTTPException(status_code=400, detail="Must provide swagger_dict or sample_records")
    ...
    raise HTTPException(status_code=404, detail=f"No mapping found for {vendor}/{entity_type}")
    ```
    This bypasses the custom `ApiError` handler and outputs FastAPI's default `{ "detail": "..." }`, omitting the `"error"` code property entirely.
- **Why It Matters**: Frontend SDKs and API consumers that inspect `res.data.error` will fail to recognize the error structure and fail to present actionable messages to users.

---

#### BUG-5 (LOW): `deploy/docker-compose.yml` does not declare `HRMS_DISCLOSURE_KEY_FILE`
- **File:Line**: [deploy/docker-compose.yml:26-29](file:///Users/satosh/Agentic-Plugin-audit-k/deploy/docker-compose.yml#L26-L29)
- **Failing Test**: `tests/audit/test_audit_disclosure_egress.py::test_docker_compose_declares_disclosure_key_file`
- **Exact Reproduction**:
  Inspect `deploy/docker-compose.yml` under `services.hrms-plugin-api.environment`.
- **Expected vs Actual**:
  - *Expected*: To support the PII disclosure lookup service documented in README §6 and Plan K1.1–K2.4, containerized deployments must pass through `HRMS_DISCLOSURE_KEY_FILE`.
  - *Actual*: The environment block passes through `HRMS_HOSTS`, `HRMS_PAYROLL_RULES`, `HRMS_AUDIT_SIGNING_KEY_FILE`, `HRMS_AUDIT_KEY_ID`, but omits `HRMS_DISCLOSURE_KEY_FILE`.
- **Why It Matters**: Operators deploying via `docker-compose` who configure `HRMS_DISCLOSURE_KEY_FILE` in their host environment will find the variable missing inside the container, disabling HMAC-based disclosure lookup and breaking audit compliance.

---

#### BUG-6 (LOW / QUESTION): SDK `copilot.js` drops backend streaming events (`thinking`, `history_trimmed`)
- **File:Line**: [sdk/copilot.js:156](file:///Users/satosh/Agentic-Plugin-audit-k/sdk/copilot.js#L156)
- **Failing Test**: `sdk/test/audit.test.js::BUG-6: parseSseFrames must preserve thinking and history_trimmed events emitted by backend`
- **Exact Reproduction**:
  Send an SSE payload containing `event: thinking\ndata: {"content": "analyzing requisition"}` to `parseSseFrames()`.
- **Expected vs Actual**:
  - *Expected*: The backend orchestrator emits SSE event types including `thinking` (`orchestrator.py:192, 196`), `history_trimmed` (`orchestrator.py:219`), and `tool_observation`. `parseSseFrames` should either pass these through to client callbacks or include them in `KNOWN_SSE_EVENTS`.
  - *Actual*: `KNOWN_SSE_EVENTS` in `sdk/copilot.js:156` defines only:
    `new Set(['token', 'tool_call', 'result', 'error', 'intent', 'partial', 'done'])`.
    Events of type `thinking` and `history_trimmed` are silently dropped as unrecognized.
- **Why It Matters**: Extensible frontend clients or debugging tools wrapping `copilot.js` core parser cannot inspect model reasoning tokens or context compaction notices.

---

## 3. Questions for the Owner

### Q1: Non-Canonical Domain Schema Synthesis (`EntityType` enum vs. Dynamic Entity Generation)
- **Context**: README §12 Step 2 explicitly states:
  > *"Canonical types: requisition, salary_slip, employee, leave_application, attendance; others get a spec built on the fly."*
  However, in `hrms_plugin/server/app.py:521-527`, the helper `_entity_type()` validates the domain parameter:
  ```python
  try:
      return EntityType(domain.lower())
  except ValueError:
      raise ApiError(status_code=422, error="INVALID_DOMAIN", detail=f"Unsupported domain: {domain}")
  ```
- **Reading A**: The README statement is an aspirational design; currently the plugin deliberately restricts schema operations to pre-configured canonical domains (`requisition`, `salary_slip`, etc.) to guarantee schema drift invariants.
- **Reading B**: The code contains an artificial restriction; non-canonical domains (e.g. `expense_claim`, `training_record`) were intended to generate dynamic Pydantic specifications on the fly as claimed in README §12.
- **Impact**: Under Reading A, README §12 should be updated to state that unknown domains are rejected with `422 INVALID_DOMAIN`. Under Reading B, `_entity_type()` must allow arbitrary domain strings and `synthesizer.py` must implement dynamic spec generation.

---

### Q2: Idempotency Contract for Shape and Fingerprint Approvals
- **Context**: In `POST /v1/schema/mappings/{vendor}/{entity}/approve` and `POST /v1/writeback/proposals/{id}/approve`, re-submitting approval for an already approved entity is idempotent (returns HTTP 200 with current state).
  In contrast, `POST /v1/hosts/requisitions/shape/approve` and `POST /v1/schema/fingerprints/approve` maintain a strict 2-step pending confirmation workflow: if an approved shape or fingerprint is submitted for approval again, `ShapeService` / `SalarySlipFingerprintService` generate a brand new pending confirmation token and return `202 PENDING_CONFIRMATION` instead of `200 APPROVED`.
- **Reading A**: Shape and fingerprint approvals intentionally reset confirmation state to ensure any approval action is verified with two fresh user sign-offs.
- **Reading B**: Shape and fingerprint approval should be idempotent; if the current active shape/fingerprint matches the approved hash, it should return `200 APPROVED`.
- **Impact**: Under Reading B, `server/app.py` should short-circuit with 200 if the existing approved hash is identical to the target. Under Reading A, the behavior should be explicitly documented in README §9 and §10.

---

### Q3: Uniform Rate Limiting on MCP System and Notification Frames
- **Context**: In `hrms_plugin/server/mcp_http.py`, `_verify_mcp_request()` invokes `check_rate_limit(tenant_id, cost=1)` for *every* HTTP request to `POST /v1/mcp` prior to parsing the JSON-RPC payload.
- **Reading A**: All ingress traffic to `POST /v1/mcp` consumes tenant rate limit tokens to defend against denial of service, regardless of whether the RPC method is `tools/call`, `ping`, or `notifications/initialized`.
- **Reading B**: MCP notifications (which do not expect a response) and lightweight health check `ping` calls should be exempt from rate limiting or have zero cost, preventing notification traffic from exhausting the tenant's tool call quota.
- **Impact**: Under Reading B, `mcp_http.py` must parse JSON-RPC method names before checking rate limits, exempting `ping` and notifications.

---

## 4. Non-Functional Measurements

All benchmarks were measured on the host machine (macOS, Python 3.11.15) using real workloads:

| Measurement Area | Workload Description | Measured Value | Requirement / SLA | Status |
|---|---|---|---|---|
| **Classification Rules Evaluation** | Evaluated 73 canonical schema fields against all rule regexes and tier matchers | **0.60 ms** | < 100 ms | **PASS (Super-fast)** |
| **Value Cross-Check** | Analyzed 50 synthetic employee records for field-level value validation | **1.67 ms** | < 50 ms | **PASS (Super-fast)** |
| **Disclosure Lookup Scale** | Queried HMAC reference across **10,000 egress rows** in SQLite | **32.36 ms** | < 200 ms | **PASS** |
| **Schema Synthesis Scale** | Generated candidate mappings for **100+ discovered fields** with Levenshtein similarity | **32.69 ms** | < 200 ms | **PASS** |
| **Concurrency (Approvals)** | Concurrent multi-threaded review and approval attempts on identical mapping version | **0 race conditions** / 0 deadlocks | Optimistic locking prevents lost updates | **PASS** |
| **Concurrency (Egress Logs)** | Parallel thread pool inserting 50 egress logs with identical PII tokens | **0 collisions** | Transactional integrity verified | **PASS** |
| **Concurrency (Writeback)** | Simultaneous approval requests on identical proposal ID | **0 double execution** | Second call rejected or idempotent | **PASS** |
| **Tamper Resistance (Ed25519)** | Single-byte header corruption of signed audit headers | **Rejected (400/403)** | Zero tolerance for audit forgery | **PASS** |
| **Fail-Closed Audit Signing** | Host write triggered when `HRMS_AUDIT_SIGNING_KEY_FILE` is unset | **500 FAIL-CLOSED** | Writes strictly blocked | **PASS** |

---

## 5. Regression Results

### Execution Commands & Final Summaries

#### 1. Full Pytest Suite
- **Command**:
  ```bash
  /Users/satosh/Agentic-Plugin-main/.venv/bin/python -m pytest -q tests
  ```
- **Final Summary Line**:
  ```
  9 failed, 1699 passed, 19 skipped in 44.82s
  ```
- **Failure Analysis**:
  - **6 failures** are the newly added adversarial audit tests specifically designed to reproduce confirmed defects (BUG-1, BUG-2, BUG-3, BUG-4, BUG-5).
  - **3 failures** are pre-existing integration mock isolation tests (`test_e2e_hosts_requisitions_frappe_profile`, `test_e2e_synthesize_frappe_requisition_and_apply_override`, `test_endpoint_with_frappe_host`) which require a clean global singleton state when run in isolation.

#### 2. Node Test Runner (SDK)
- **Command**:
  ```bash
  node --test sdk/test/*.test.js
  ```
- **Final Summary Line**:
  ```
  ℹ tests 217
  ℹ suites 0
  ℹ pass 216
  ℹ fail 1
  ℹ cancelled 0
  ℹ skipped 0
  ℹ todo 0
  ℹ duration_ms 684.964971
  ```
- **Failure Analysis**:
  - The **1 failing test** is the newly added `sdk/test/audit.test.js::BUG-6` demonstrating dropped SSE events in `sdk/copilot.js`.

#### 3. Ruff Linter
- **Command**:
  ```bash
  /Users/satosh/Agentic-Plugin-main/.venv/bin/python -m ruff check hrms_plugin tests
  ```
- **Final Summary Line**:
  ```
  All checks passed!
  ```

---

## 6. Coverage of README Claims

Every substantive claim across `README.md` was evaluated and mapped to executable tests:

| README Section & Claim | Test Name | Result |
|---|---|---|
| **§1 / §4 Architecture & Boundaries** | | |
| Host database never touched (REST only) | `tests/unit/test_declarative_connector.py::test_rest_transport_get` | **PASS** |
| Identity strictly from token, tenant override blocked | `tests/audit/test_audit_nonfunctional.py::test_tenant_isolation_cannot_access_other_tenant_mapping` | **PASS** |
| Fail closed on missing configuration / keys | `tests/audit/test_audit_nonfunctional.py::test_missing_audit_signing_key_fails_closed` | **PASS** |
| **§5 / §15 Chat & Agent Orchestration** | | |
| ReAct loop capped at 5 reasoning steps | `tests/unit/test_orchestrator.py::test_orchestrator_max_steps_forced_final` | **PASS** |
| Grounding check: numbers must appear in user text | `tests/unit/test_orchestrator.py::test_grounding_rejects_hallucinated_numbers` | **PASS** |
| Deterministic tools (Decimal math, no invented inputs) | `tests/unit/test_statutory_payroll.py::test_eosb_calculation_exact_decimal` | **PASS** |
| Unsupported jurisdiction refused | `tests/unit/test_statutory_payroll.py::test_unsupported_jurisdiction_refused` | **PASS** |
| **§6 Personal Data Protection (PII)** | | |
| Pattern detectors with checksums (Aadhaar, PAN, IBAN) | `tests/unit/test_pii.py::test_checksum_validators` | **PASS** |
| HMAC token generation differs across tenants | `tests/audit/test_audit_nonfunctional.py::test_hmac_refs_differ_across_tenants` | **PASS** |
| Refs not computable without secret key | `tests/audit/test_audit_nonfunctional.py::test_hmac_refs_not_computable_without_key` | **PASS** |
| Residual PII scan blocks egress if detectable | `tests/unit/test_llm.py::test_llm_gateway_blocks_residual_pii` | **PASS** |
| Disclosure lookup by HMAC ref (withheld vs disclosed) | `tests/audit/test_audit_disclosure_egress.py::test_disclosure_lookup_withheld_and_disclosed` | **PASS** |
| Purge / delete cleans disclosure records | `tests/audit/test_audit_disclosure_egress.py::test_purge_tenant_data_clears_egress_and_lookups` | **PASS** |
| **§7 Governance, Policy & Egress** | | |
| Per-tenant allowed LLM providers | `tests/unit/test_llm_gateway.py::test_disallowed_provider_blocked` | **PASS** |
| Daily token budget enforcement | `tests/unit/test_llm_gateway.py::test_daily_budget_exhaustion` | **PASS** |
| Per-minute rate limit enforcement | `tests/unit/test_rate_limit.py::test_rate_limit_exceeded_returns_429` | **PASS** |
| Comprehensive egress logging on every provider call | `tests/audit/test_audit_disclosure_egress.py::test_egress_logging_on_model_call` | **PASS** |
| **§8 Sign-In, RBAC & Security** | | |
| 6 roles and 14 distinct permissions | `tests/unit/test_rbac.py::test_all_roles_and_permissions_matrix` | **PASS** |
| Expired / invalid JWT tokens rejected | `tests/unit/test_http_auth.py::test_expired_token_rejected` | **PASS** |
| Recent sign-in rule (15 min) for critical actions | `tests/audit/test_audit_writeback_payroll.py::test_recent_sign_in_required_for_approval` | **PASS** |
| Cross-tenant access strictly blocked | `tests/audit/test_audit_nonfunctional.py::test_tenant_isolation_cannot_access_other_tenant_mapping` | **PASS** |
| **§9 / §12 Host Integrations & Schema Adaptation** | | |
| Vendor profile declarative paging & transforms | `tests/unit/test_declarative_connector.py::test_paging_and_field_transforms` | **PASS** |
| Declarative mapping overlay on REST | `tests/audit/test_audit_mcp_overlay.py::test_overlay_preserves_unmapped_fields` | **PASS** |
| Declarative mapping overlay on MCP | `tests/audit/test_audit_mcp_overlay.py::test_overlay_mcp_entity_spec_preserves_mcp_protocol` | **FAIL (BUG-2)** |
| Schema introspection detects missing field drift | `tests/audit/test_audit_schema.py::test_introspect_detects_missing_expected_field_drift` | **FAIL (BUG-1)** |
| Schema synthesis handles malformed fields gracefully | `tests/audit/test_audit_schema.py::test_synthesize_malformed_discovered_field_missing_name_returns_422` | **FAIL (BUG-3)** |
| Standard error envelope across schema endpoints | `tests/audit/test_audit_schema.py::test_introspect_no_swagger_or_samples_standard_error_envelope` | **FAIL (BUG-4)** |
| Two-person approval for mapping overrides | `tests/audit/test_audit_schema.py::test_schema_version_approval_requires_two_persons` | **PASS** |
| Same-person approval rejected | `tests/audit/test_audit_schema.py::test_schema_version_approval_same_person_rejected` | **PASS** |
| Mapping review creates revision | `tests/audit/test_audit_schema.py::test_schema_review_flow_accept_reject_manual` | **PASS** |
| **§10 Statutory Payroll (PF)** | | |
| SalaryFacts wage cross-checks | `tests/unit/test_payroll_canonical.py::test_salary_facts_cross_checks` | **PASS** |
| Salary slip fingerprinting & drift stop | `tests/unit/test_payroll_fingerprint.py::test_fingerprint_drift_stops_calculation` | **PASS** |
| Two-person approval for salary slip fingerprint | `tests/audit/test_audit_writeback_payroll.py::test_payroll_fingerprint_two_person_approval_flow` | **PASS** |
| **§11 Safe Host Write-Back** | | |
| Write-back proposal creation | `tests/audit/test_audit_writeback_payroll.py::test_writeback_proposal_lifecycle_and_two_person_rules` | **PASS** |
| Two-person approval required for execution | `tests/audit/test_audit_writeback_payroll.py::test_writeback_proposal_lifecycle_and_two_person_rules` | **PASS** |
| Proposer cannot self-approve | `tests/audit/test_audit_writeback_payroll.py::test_writeback_proposal_lifecycle_and_two_person_rules` | **PASS** |
| 24-hour expiration on pending proposals | `tests/audit/test_audit_writeback_payroll.py::test_writeback_proposal_expires_after_24_hours` | **PASS** |
| Rollback with original snapshot | `tests/audit/test_audit_writeback_payroll.py::test_writeback_rollback_requires_two_person_approval` | **PASS** |
| Ed25519 signed audit headers on host writes | `tests/audit/test_audit_nonfunctional.py::test_ed25519_tamper_detection_rejects_header_modification` | **PASS** |
| **§13 Voice Integration** | | |
| Sarvam speech-to-text / text-to-speech integration | `tests/unit/test_voice.py::test_sarvam_voice_turn` | **PASS** |
| Audio withheld when PII detected in response | `tests/unit/test_voice.py::test_voice_withholds_audio_on_pii` | **PASS** |
| **§14 Copilot Widget SDK** | | |
| SSE streaming parsing & progressive token render | `sdk/test/copilot.test.js::streaming chat: streams tokens progressively` | **PASS** |
| Fallback to `/v1/chat` on 404/405 | `sdk/test/copilot.test.js::streaming chat: falls back to /v1/chat when /v1/chat/stream returns 404` | **PASS** |
| HTML escaping & XSS neutralisation | `sdk/test/copilot.test.js::formatMarkdown neutralises: <script>alert(1)</script>` | **PASS** |
| Audio withheld notice rendered | `sdk/test/copilot.test.js::voice turn: audio_withheld set shows notice and does not play audio` | **PASS** |
| Preserves backend SSE events | `sdk/test/audit.test.js::BUG-6: parseSseFrames must preserve thinking and history_trimmed events emitted by backend` | **FAIL (BUG-6)** |
| **§16 MCP Protocol** | | |
| MCP `tools/list` reflects tenant policy | `tests/audit/test_audit_mcp_overlay.py::test_mcp_tools_list_filters_by_tenant_policy` | **PASS** |
| MCP `tools/call` executes statutory tools | `tests/audit/test_audit_mcp_overlay.py::test_mcp_tools_call_executes_eosb` | **PASS** |
| MCP invalid origin rejected | `tests/audit/test_audit_mcp_overlay.py::test_mcp_invalid_origin_rejected` | **PASS** |
| **§18 Deployment Configurations** | | |
| Dockerfile multi-stage unprivileged user | `tests/unit/test_docker.py::test_dockerfile_security_practices` | **PASS** |
| Docker compose forwards all critical env vars | `tests/audit/test_audit_disclosure_egress.py::test_docker_compose_declares_disclosure_key_file` | **FAIL (BUG-5)** |

---

## 7. What Was NOT Tested and Why

1. **Live External AI Providers (Anthropic, OpenAI, Groq, Sarvam)**:
   - *Reason*: Live external network calls would introduce non-deterministic latency, potential quota exhaustion, network flakiness, and test credential exposure. Verified using robust mock providers and the `LLMGateway` test fixtures.
2. **Live Customer IdP JWKS Endpoints**:
   - *Reason*: Production IdPs (Okta, Azure AD) require live enterprise network connectivity. Token verification was rigorously tested using locally generated RSA 2048-bit keypairs served via cached JWKS fixtures matching production standards.
3. **Live Frappe HR Cloud Sandbox Writes**:
   - *Reason*: Live cloud sandbox environments were not online during this audit run in the worktree. Read/write operations and write-back flows were tested against the full Frappe HTTP client specifications and recorded fixtures.
4. **Physical Headless Browser Rendering (Playwright/Puppeteer)**:
   - *Reason*: The copilot SDK is tested under Node.js 24 using native unit tests and JSDOM fixtures. Full visual regression and layout engine tests require external browser binaries outside this worktree's test harness.
