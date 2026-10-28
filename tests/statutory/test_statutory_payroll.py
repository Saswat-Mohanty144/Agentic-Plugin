"""Tier 3 Invariant Tests: Statutory Payroll and Decimal Precision Arithmetic."""

import pytest

from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent


@pytest.fixture
def payroll_agent():
    return StatutoryPayrollAgent()


def test_india_salary_structure_decimal_exactness(payroll_agent):
    """Verify that all salary components in India CTC sum exactly without floating point drift."""
    annual_ctc = 1200000.0  # 12 Lakhs INR
    res = payroll_agent.structure_india_salary(annual_ctc=annual_ctc, basic_percent=40.0, hra_percent=20.0)

    # Monthly CTC must be exactly 100,000.00
    assert res.monthly_ctc == 100000.0
    assert res.basic_salary == 40000.0
    assert res.hra == 20000.0

    # Employee PF = 12% of 40,000 = 4,800.00
    assert res.employee_pf == 4800.0
    # Employer PF total = 12% of 40,000 = 4,800.00 (EPS: 8.33% of 15000 = 1249.50, EPF remainder = 3550.50)
    assert res.employer_eps == 1249.50
    assert res.employer_epf == 3550.50
    assert res.employer_pf_total == 4800.0

    # Gratuity monthly provision = (15 / (26 * 12)) * 40000 = 1923.08
    assert res.gratuity_monthly_provision == 1923.08

    # Gross = Monthly CTC - Employer PF - Gratuity Provision = 100000 - 4800 - 1923.08 = 93276.92
    assert res.gross_salary == 93276.92

    # Special Allowance = Gross - Basic - HRA = 93276.92 - 40000 - 20000 = 33276.92
    assert res.special_allowance == 33276.92

    # Net Take-Home = Gross - Employee PF (4800) - PT (200) = 93276.92 - 4800 - 200 = 88276.92
    assert res.net_take_home == 88276.92

    # Statutory citations must be populated
    assert len(res.statutory_citations) >= 2
    assert any("Provident" in cit for cit in res.statutory_citations)


def test_india_salary_structure_esi_applicability(payroll_agent):
    """Verify that ESI (0.75% employee, 3.25% employer) applies when gross <= 21,000 INR."""
    annual_ctc = 240000.0  # 20,000 / month CTC
    res = payroll_agent.structure_india_salary(annual_ctc=annual_ctc, basic_percent=50.0, hra_percent=25.0)

    # Gross is below 21,000 INR ceiling -> ESI must be triggered
    assert res.gross_salary <= 21000.0
    assert res.employee_esi > 0.0
    assert res.employer_esi > 0.0

    # Invariant: employee ESI is 0.75% of gross rounded
    expected_emp_esi = round(res.gross_salary * 0.0075, 2)
    assert abs(res.employee_esi - expected_emp_esi) <= 0.01


def test_uae_salary_structure(payroll_agent):
    """Verify UAE WPS-compliant salary structure."""
    monthly_gross = 25000.0  # 25,000 AED
    res = payroll_agent.structure_uae_salary(monthly_gross=monthly_gross, basic_percent=60.0, housing_percent=30.0)

    assert res.monthly_gross == 25000.0
    assert res.basic_wage == 15000.0  # 60% of 25k
    assert res.housing_allowance == 7500.0  # 30% of 25k
    assert res.transport_allowance == 2500.0  # 10% of 25k
    assert res.wps_compliant is True
    assert res.estimated_monthly_eosb_accrual > 0.0
    assert len(res.statutory_citations) >= 2


def test_uae_eosb_gratuity_invariants(payroll_agent):
    """Verify UAE End of Service Severance Gratuity (Article 51) calculations across tenure tiers."""
    monthly_basic = 12000.0  # Daily wage = 12000 / 30 = 400 AED

    # Tier 1: Under 1 year service -> 0 AED
    res_0_8 = payroll_agent.calculate_uae_eosb(basic_wage_monthly=monthly_basic, tenure_years=0.8)
    assert res_0_8.total_eosb_gratuity == 0.0

    # Tier 2: 3 years service -> 3 * 21 days * 400 AED = 25,200 AED
    res_3 = payroll_agent.calculate_uae_eosb(basic_wage_monthly=monthly_basic, tenure_years=3.0)
    assert res_3.total_eosb_gratuity == 25200.0

    # Tier 3: 7 years service -> (5 * 21 * 400) + (2 * 30 * 400) = 42,000 + 24,000 = 66,000 AED
    res_7 = payroll_agent.calculate_uae_eosb(basic_wage_monthly=monthly_basic, tenure_years=7.0)
    assert res_7.total_eosb_gratuity == 66000.0

    # Tier 4: Maximum Cap -> 2 years basic wage = 24 * 12,000 = 288,000 AED
    res_30 = payroll_agent.calculate_uae_eosb(basic_wage_monthly=monthly_basic, tenure_years=30.0)
    assert res_30.total_eosb_gratuity == 288000.0
    assert "Capped at 2 years" in res_30.formula_breakdown
