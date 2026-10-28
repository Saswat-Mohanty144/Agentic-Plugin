# Resolution of agy findings (2026-10-28-agy-findings.json)

agy run: gemini-3.8-flash-high, verdict FAIL, 6 findings, 22 requirements checked. agy touched no files except its report (HEAD unchanged, `git status` showed only the JSON).
Each finding was reproduced by Claude before acceptance. None disputed.

| ID | Sev | Reproduced | Status | Fix commit / evidence |
|---|---|---|---|---|
| F1 | HIGH | Yes — `process_message("run compliance audit", context={"tenant_id":"malicious_corp"})` replied "Compliance Audit completed for malicious_corp" | FIXED | RED: 3 failed → GREEN 26 passed. `supervisor.py` compliance branch now uses `auth.tenant_id`. Sweep: only other request-sourced tenant is `mcp_server.py:192` (MCP has no transport; out of scope per D5, carried to piece F). |
| F3 | HIGH | Yes — neither deploy/docker-compose.yml nor deploy/k8s/deployment.yaml set HRMS_TENANT_REGISTRY or mounted a file | FIXED | RED: 2 failed (new tests) → GREEN 5 passed; proven red again with deploy files stashed. Compose: env + `${HRMS_TENANT_REGISTRY_FILE:?…}:/etc/hrms/tenants.yaml:ro` (`docker compose config` exits 1 when unset; renders the mount when set). k8s: env + read-only mount of ConfigMap `hrms-plugin-tenant-registry` (key tenants.yaml). Registry CONTENT is user-owned: NEEDS_HUMAN — create the ConfigMap / host file with real issuers. One test-parser bug of my own fixed (rsplit on ':'), no assertion weakened. |
| F2 | MEDIUM | Yes — 7 tool schemas advertised a jurisdiction `default`; 5 omitted it from `required` | FIXED | RED: 8 failed → GREEN. Removed 7 jurisdiction defaults + the audit tool's `tenant_id` default (extension: same server-injected class); added jurisdiction to `required` on 5 tools. Characterisation snapshot re-recorded after a programmatic diff showed ONLY these 13 schema deltas (archetype output and descriptions identical). Noted for later (not in scope): EOSB/final-settlement schema enums are only [AE, IN] while the API accepts SA/US — part of the known US/SA payroll gap. |
| F4 | MEDIUM | Yes — `env -u HRMS_TENANT_REGISTRY python -c 'from hrms_plugin.server.app import create_app'` raised RegistryError | FIXED | RED: 1 failed → GREEN 19 passed. Default `app` now built lazily via module `__getattr__` and cached. Kept: accessing `app` / starting the service without a registry still fails (exit 1, names the env var). Live boot re-verified (healthz 200, remediation without token 401). |
| F5 | LOW | Yes — test sent X-Tenant-Id equal to the token tenant | FIXED (test-only) | New test: no header, tenants acme AND globex. Proof against a mutant hard-coding `tenant_id="acme"` in the remediation handler: OLD test passed (vacuous), NEW test failed; NEW test passes on real code. Mutant reverted. |
| F6 | LOW | Yes — test called only `required_permission`, never `process_message` | FIXED (test-only) | Added 2×13 parametrised tests forcing each UserIntent through `process_message` (zero roles → PermissionDenied with the exact required permission; SUPER_ADMIN → reply). Proof: with the gate removed, all 13 deny tests fail. Gate restored. |

## Outcome
- All 6 findings reproduced and fixed; 0 disputed. Per DEV_LOOP cap there is no second agy round; each fix is proven by its red→green (or mutant-kill) evidence above and by re-running acceptance.
- NEEDS_HUMAN: supply real tenant registry content (ConfigMap `hrms-plugin-tenant-registry` / `HRMS_TENANT_REGISTRY_FILE`).
- Carried to later pieces: MCP server reads tenant from tool arguments (piece F); EOSB/settlement schema enums lack SA/US (US/SA payroll gap).
