# Piece I — second vendor via configuration, build-vs-buy (Q5, plan Phase 11)

Status: decided by architect under user delegation (2026-10-29). Base: piece H branch.
Gate (docs/PHASE_WISE_PLAN.md Phase 11): a structurally different vendor is onboarded with configuration plus at most
one code hook; the build-vs-buy decision is recorded as an ADR.

## Evidence gathered (no assumptions)
- Frappe HRMS `Job Requisition` doctype, source
  `frappe/hrms@develop hrms/hr/doctype/job_requisition/job_requisition.json` (fetched 2026-10-29): fields used here —
  `designation` (Link, reqd), `no_of_positions` (Int, reqd), `status` (Select: Pending, Open & Approved, Rejected,
  Filled, On Hold, Cancelled), `requested_by` (Link Employee), `department` (Link), `description` (Text Editor);
  name from naming series `HR-HIREQ-`. REST conventions as in piece F1 (`/api/resource/:doctype`, `fields` and
  `limit_start`/`limit_page_length`, rows under `data`, `Authorization: token key:secret`).
- iCeipts, `tests/fixtures/iceipts_swagger.yaml`: `GET /job/getAllRequisition/{userId}` (bearer JWT; query `page`,
  `size` 1–100; rows at `data.pageData`; `data.totalPages`); `JobRequisitionDetails`: `id`, `title`, `status`
  (DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, JOB_CONVERTED, CLOSED), `location`, `position` (string, e.g. "3"),
  `jobDescription`.
- Payroll for a second vendor: no public, verifiable payslip API found for Zoho Payroll (India) or Keka (searches
  2026-10-29 returned UI help pages only); iCeipts swagger has no payroll paths → second-vendor PF stays NEEDS_HUMAN.

## Structural differences the engine must absorb
| | Frappe | iCeipts |
|---|---|---|
| Auth | `token key:secret` | `Bearer <JWT>` |
| Path | fixed doctype path | service user id in the path (host config `service_user_id`) |
| Paging | offset (`limit_start`, `limit_page_length`), stop on short page | page number, `totalPages` |
| Envelope | `data` | `data.pageData` |
| Field selection | must request `fields` explicitly | full objects |
| Headcount | int | numeric string |
| Status vocabulary | 6 title-case values | 6 upper-case values |

## Decisions
- I1 Declarative profile (YAML, strict Pydantic, `extra="forbid"`) per vendor in `hrms_plugin/integrations/profiles/`:
  auth scheme (`frappe_token` | `bearer`), entity → list request (path template, static query, pagination kind and
  params, items path, id field, whether to request fields), field map (`from` dotted path, optional `type: int|str`,
  optional `map` for enumerations). Paths may only reference declared params; every param is URL-encoded.
- I2 No code hook. The iCeipts swagger does not document the JWT's claims, so the plugin does not decode the token
  to guess a user id (the old `extract_jwt_user_id` tries four claim names). The `{user_id}` path parameter comes from
  host-registry config `service_user_id` (UUID), the `user.id` returned by `POST /employee/login` for the service
  account (documented in the swagger). Path params may only come from host config; profiles name them, never values.
- I3 Canonical requisition status vocabulary: PENDING, OPEN, REJECTED, FILLED, ON_HOLD, CANCELLED, DRAFT, CLOSED.
  HR-domain mapping: Frappe `Open & Approved`→OPEN; iCeipts `APPROVED`→OPEN, `JOB_CONVERTED`→OPEN (approved and
  published as a job opening = still hiring), `PENDING_APPROVAL`→PENDING. An unmapped vendor value never passes
  through: the record is rejected and reported (`UNMAPPED_VALUE`), the rest are returned.
- I4 `headcount` must be a positive integer after conversion; otherwise the record is rejected (`INVALID_VALUE`).
- I5 Safety bounds: max 50 pages, page size from the profile (≤100), https base URL only (host registry), timeouts
  from the host registry; non-2xx → `HOST_ERROR`; response shape mismatch → `INCONSISTENT_PAYLOAD`.
- I6 Host registry: `vendor: frappe | iceipts`. Frappe needs `api_key_env` + `api_secret_env`; iCeipts needs
  `token_env` and `service_user_id` (UUID). `writable_fields` and payroll stay Frappe-only: payroll/write-back on another vendor returns
  `VENDOR_NOT_SUPPORTED` instead of attempting a Frappe call.
- I7 Exposure: `GET /v1/hosts/requisitions` (permission MANAGE_RECRUITMENT, tenant from token) →
  `{vendor, requisitions: [...canonical], rejected: [{id, error, field}]}`. No LLM involved.
- I8 Build-vs-buy: ADR `docs/adr/0001-build-vs-buy-hris-connectors.md`, status **Proposed**; options, criteria and
  the time-boxed evaluation protocol are written; the decision itself is NEEDS_HUMAN (owner, cost, contracts).

## Out of scope / NEEDS_HUMAN
- iCeipts service-account token issuance/rotation and its `service_user_id`; real tenant host entries; second-vendor payroll API spec;
  the build-vs-buy decision; evaluation accounts for a unified API (e.g. Merge/Finch/Kombo) and Nango.
