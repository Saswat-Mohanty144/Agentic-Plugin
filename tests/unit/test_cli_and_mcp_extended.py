"""Unit tests for the CLI utility and extended MCP tool suite."""

from hrms_plugin.cli import main
from hrms_plugin.server.mcp_server import HrmsMcpServer


def test_cli_calculate_eosb(capsys):
    ret = main(["calculate-eosb", "--basic", "15000", "--tenure", "4.5"])
    assert ret == 0
    captured = capsys.readouterr().out
    assert "UAE Statutory EOSB Calculation" in captured
    assert "Gratuity Payable: AED 47,250.00" in captured


def test_cli_calculate_salary_india(capsys):
    ret = main(["calculate-salary", "--ctc", "1200000", "--jurisdiction", "IN"])
    assert ret == 0
    captured = capsys.readouterr().out
    assert "India CTC Breakdown" in captured
    assert "Annual CTC: INR 1,200,000.00" in captured


def test_cli_check_bias(capsys):
    ret = main(["check-bias", "--text", "Looking for a rockstar developer who is a digital native"])
    assert ret == 0
    captured = capsys.readouterr().out
    assert "Recruitment Bias & Inclusivity Audit" in captured
    assert "Biased Terms Found: 1" in captured


def test_mcp_extended_tools():
    """P11: over MCP, number-taking calculators are refused and lookups run through the validated registry."""
    import asyncio

    from hrms_plugin.security.rbac import AuthContext, UserRole

    mcp = HrmsMcpServer(AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN})),
                        jurisdiction="AE")
    names = {t.name for t in mcp.list_tools()}
    assert "hrms_search_labor_regulations" in names and "hrms_audit_job_description_bias" in names
    for refused in ("hrms_calculate_eosb_gratuity", "hrms_score_candidate_ats", "hrms_detect_travel_anomaly",
                    "hrms_evaluate_leave_request"):
        assert refused not in names
        assert asyncio.run(mcp.call_tool(refused, {}))["error"] == "TOOL_NOT_AVAILABLE"
    bias = asyncio.run(mcp.call_tool("hrms_audit_job_description_bias", {"jd_text": "Looking for rockstar ninja"}))
    assert bias["has_bias"] is True
