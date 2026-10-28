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
    mcp = HrmsMcpServer()
    tools = mcp.list_tools()
    tool_names = [t.name for t in tools]

    assert "hrms_score_candidate_ats" in tool_names
    assert "hrms_audit_job_description" in tool_names
    assert "hrms_evaluate_leave_request" in tool_names
    assert "hrms_detect_impossible_travel" in tool_names

    # Test ATS scoring MCP tool
    ats_res = mcp.call_tool(
        "hrms_score_candidate_ats",
        {
            "resume_text": "Experienced Python developer with 4 years building FastAPI microservices.",
            "required_skills": ["Python", "FastAPI"],
            "required_experience_years": 3.0,
            "candidate_experience_years": 4.0,
        },
    )
    assert ats_res["composite_score"] >= 70

    # Test Impossible travel MCP tool
    punch1 = {"lat": 19.0760, "lng": 72.8777, "timestamp": "2026-09-24T09:00:00Z"}
    punch2 = {"lat": 25.2048, "lng": 55.2708, "timestamp": "2026-09-24T10:00:00Z"}
    travel_res = mcp.call_tool("hrms_detect_impossible_travel", {"punch1": punch1, "punch2": punch2})
    assert travel_res["is_anomaly"] is True
