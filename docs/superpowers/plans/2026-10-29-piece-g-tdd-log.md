# TDD log — piece G (governance)

Branch feat/piece-g-governance (stacked on F2 @ bea4cce).

## G-a — egress log, tenant LLM policy (fail closed), compensation tokenisation (G1-G3)
- Test fixture registry: acme and globex get `llm_policy: {allowed_providers: [fake]}`; initech deliberately has none (exercises fail-closed).
- RED: `ModuleNotFoundError: No module named 'hrms_plugin.governance'`.
- GREEN: test_governance_llm.py 13 passed; full suite 970. Env-built gateways (all production paths) always enforce policy and log; explicit-config gateways only when given a resolver/log (proved by test_env_built_gateway_enforces_policy). Policy is checked before anything is sent (provider never called); stream returns plain 403 via the pre-check.
- Mutation: fail-open when no policy → 4 failed; ignoring the compensation switch → 1 failed. Both killed.
