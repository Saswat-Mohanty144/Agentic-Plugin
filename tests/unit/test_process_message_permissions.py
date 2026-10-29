"""process_message enforces the same permissions as the tools (spec §6.5a)."""

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent, required_permission
from hrms_plugin.security.rbac import AuthContext, PermissionDenied
from hrms_plugin.security.rbac import Permission as P
from hrms_plugin.security.rbac import UserRole as R

SETTLEMENT_MSG = "Compute final settlement for exiting employee"
ONBOARD_DOCS_MSG = "Verify onboarding documents KYC for new joiner"
ONBOARD_OFFER_MSG = "Evaluate onboarding offer for new joiner"


def auth(*roles):
    return AuthContext(user_id="u", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("intent", list(UserIntent))
def test_every_intent_has_a_permission(intent):
    assert isinstance(required_permission(intent, ""), P)


def test_onboarding_splits_by_action():
    assert required_permission(UserIntent.ONBOARDING, "verify kyc document") is P.VIEW_EMPLOYEE
    assert required_permission(UserIntent.ONBOARDING, "evaluate the offer") is P.MANAGE_RECRUITMENT


@pytest.mark.parametrize("msg,intent", [
    (SETTLEMENT_MSG, UserIntent.OFFBOARDING_FNF),
    (ONBOARD_DOCS_MSG, UserIntent.ONBOARDING),
    (ONBOARD_OFFER_MSG, UserIntent.ONBOARDING),
])
def test_precondition_messages_classify_as_expected(msg, intent):
    assert SupervisorAgent().classify_intent(msg)[0] == intent


def test_employee_denied_final_settlement():
    with pytest.raises(PermissionDenied) as info:
        SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE", auth=auth(R.EMPLOYEE))
    assert info.value.permission is P.VIEW_SALARY


def test_hr_admin_allowed_final_settlement():
    assert SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE", auth=auth(R.HR_ADMIN)).reply_text


def test_employee_allowed_onboarding_documents_but_not_offer():
    agent = SupervisorAgent()
    assert agent.process_message(ONBOARD_DOCS_MSG, jurisdiction="IN", auth=auth(R.EMPLOYEE)).reply_text
    with pytest.raises(PermissionDenied) as info:
        agent.process_message(ONBOARD_OFFER_MSG, jurisdiction="IN", auth=auth(R.EMPLOYEE))
    assert info.value.permission is P.MANAGE_RECRUITMENT


def test_auth_is_required():
    with pytest.raises(TypeError):
        SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE")


def test_http_non_agentic_and_legacy_paths_return_403(make_client, bearer):
    client = make_client()
    h = bearer(roles=["staff"])
    res = client.post("/v1/chat", headers=h, json={"message": SETTLEMENT_MSG, "jurisdiction": "AE", "agentic": False})
    assert res.status_code == 403 and res.json() == {"error": "FORBIDDEN", "permission": "VIEW_SALARY"}
    res = client.post("/api/compliance/agent/chat", headers=h, json={"message": SETTLEMENT_MSG, "jurisdiction": "AE"})
    assert res.status_code == 403 and res.json() == {"error": "FORBIDDEN", "permission": "VIEW_SALARY"}


def test_http_hr_admin_non_agentic_ok(make_client, bearer):
    res = make_client().post("/v1/chat", headers=bearer(roles=["hr_manager"]),
                             json={"message": SETTLEMENT_MSG, "jurisdiction": "AE", "agentic": False})
    assert res.status_code == 200


# agy F1 — tenant must come from AuthContext, never from request context ---------------------

def test_compliance_audit_uses_token_tenant_not_context(monkeypatch):
    agent = SupervisorAgent()
    seen = {}
    original = agent.compliance_agent.audit_employees

    def spy(*args, **kwargs):
        seen["tenant_id"] = kwargs.get("tenant_id")
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.compliance_agent, "audit_employees", spy)
    resp = agent.process_message("run compliance audit", context={"tenant_id": "malicious_corp"}, auth=auth(R.HR_ADMIN))
    assert seen["tenant_id"] == "acme"
    assert "malicious_corp" not in resp.reply_text


def test_compliance_audit_without_context_tenant_uses_token_tenant(monkeypatch):
    agent = SupervisorAgent()
    seen = {}
    original = agent.compliance_agent.audit_employees
    monkeypatch.setattr(agent.compliance_agent, "audit_employees",
                        lambda *a, **k: seen.setdefault("tenant_id", k.get("tenant_id")) and original(*a, **k))
    agent.process_message("run compliance audit", context={}, auth=auth(R.HR_ADMIN))
    assert seen["tenant_id"] == "acme"


def test_http_non_agentic_audit_ignores_context_tenant(make_client, bearer):
    res = make_client().post("/v1/chat", headers=bearer(roles=["hr_manager"]), json={
        "message": "run compliance audit", "jurisdiction": "IN", "agentic": False,
        "context": {"tenant_id": "malicious_corp"}})
    assert res.status_code == 200
    assert "malicious_corp" not in res.json()["reply_text"]
    assert "acme" in res.json()["reply_text"]


# agy F6 — the gate is exercised through process_message itself, for every intent --------------

@pytest.mark.parametrize("intent", list(UserIntent))
def test_process_message_denies_every_intent_without_permission(monkeypatch, intent):
    agent = SupervisorAgent()
    monkeypatch.setattr(agent, "classify_intent", lambda message: (intent, 0.99))
    with pytest.raises(PermissionDenied) as info:
        agent.process_message("anything", jurisdiction="IN", auth=auth())
    assert info.value.permission is required_permission(intent, "anything")


@pytest.mark.parametrize("intent", list(UserIntent))
def test_process_message_allows_every_intent_with_permission(monkeypatch, intent):
    agent = SupervisorAgent()
    monkeypatch.setattr(agent, "classify_intent", lambda message: (intent, 0.99))
    assert agent.process_message("anything", jurisdiction="IN", auth=auth(R.SUPER_ADMIN)).reply_text
