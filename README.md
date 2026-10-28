# HRMS Agentic Plugin (`hrms-agentic-plugin`)

A standalone, host-agnostic intelligence microservice that makes **any HRMS agentic** without touching the host database or modifying core application code.

Plugs into `iceipts_hrms`, Frappe/ERPNext, BambooHR, Darwinbox, Workday, or custom in-house REST APIs via an autonomous **Dynamic Schema Adaptation Engine**, providing 14 specialized domain agents, statutory legal compliance RAG, and an embeddable copilot widget.

---

## Documentation Quick Links

All detailed architectural specifications, blueprints, and implementation roadmaps are located in the [**`docs/`**](docs/) directory:

- 📄 [**`docs/PHASE_WISE_PLAN.md`**](docs/PHASE_WISE_PLAN.md) — **Master Phase-Wise Implementation Plan & Testing Strategy**
  - Section 1: Product Overview & Architectural Boundary
  - Section 2: Communication & Protocol Specification
  - Section 3: The 5-Stage Dynamic Schema Engine
  - Section 4: Phase-by-Phase Engineering Roadmap (Phases 1 through 6)
  - Section 5: The 5-Tier Production Quality Pyramid
  - Section 6: Distribution & Deployment Topologies (Cloud SaaS, On-Prem Container, Standalone MCP Server)
  - Section 7: Production Readiness & Acceptance Checklist

---

## Core Capabilities

1. **Dynamic Schema Adaptation**:
   - Automated introspection of OpenAPI 3.x, Swagger 2.0, and JSON record samples.
   - Semantic alignment to Canonical Domain Models (`CanonicalEmployee`, `CanonicalLeaveRequest`, `CanonicalRequisition`).
   - Shadow verification probing with quality grading (0.0 to 1.0).
   - High-speed declarative `FieldMap` compilation executing in `< 1ms`.

2. **14 Specialized Domain Agents**:
   - Recruitment & ATS scoring, Leave & Attendance anomaly detection, Decimal-exact Statutory Payroll (India, UAE, US), Compliance Auditing with mandatory gazette citations, and Voice AI (Sarvam AI).

3. **Enterprise Zero-Trust Governance**:
   - Dynamic pre-prompt PII redaction and vault tokenization.
   - Action attribution headers (`X-Agent-Invocation-Id`, `X-HITL-Approver-Id`, `X-Reasoning-Hash`) compatible with host audit loggers (`auditLogger.js`).
   - Human-in-the-loop (HITL) review gates with side-by-side visual diffs.

4. **Embeddable Frontend Copilot SDK**:
   - Single `<script src="copilot.min.js">` tag (< 120KB gzipped).
   - Shadow DOM rendering to eliminate CSS bleeding into host layouts.
   - Route and record context synchronization via `postMessage`.

---

## Project Structure

```
hrms-agentic-plugin/
├── docs/                          # Architectural specs & phase plans
│   ├── PHASE_WISE_PLAN.md         # Master phase-by-phase implementation plan
│   └── README.md                  # Documentation index
│
├── deploy/                        # Docker, Helm & deployment templates
├── hrms_plugin/                   # Core Python package (schema, agents, connectors)
├── sdk/                           # Embeddable Web Component & Copilot drawer
└── tests/                         # 5-Tier testing pyramid (Unit, Contract, Statutory, Drift, E2E)
```
