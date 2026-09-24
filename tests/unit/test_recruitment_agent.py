"""Unit tests for Recruitment Specialist Agent."""

import pytest
from hrms_plugin.agents.recruitment import RecruitmentAgent


@pytest.fixture
def recruitment_agent():
    return RecruitmentAgent()


def test_job_description_bias_audit(recruitment_agent):
    """Test detection and rewriting of exclusionary / aggressive keywords in job descriptions."""
    biased_jd = (
        "We are looking for a rockstar developer and ninja coder who is aggressive in problem solving. "
        "Must be willing to work hard play hard with our team."
    )

    res = recruitment_agent.audit_job_description_bias(biased_jd)
    assert res.has_bias is True
    assert len(res.detected_terms) >= 3
    flagged = [d["flagged_term"] for d in res.detected_terms]
    assert "rockstar" in flagged
    assert "ninja" in flagged
    assert "aggressive" in flagged
    assert "[exceptional contributor / skilled specialist]" in res.suggested_rewrite


def test_ats_candidate_scoring(recruitment_agent):
    """Test ATS candidate scoring with skills matching, experience ratio, and recommendation band."""
    resume = (
        "Senior Backend Engineer with 5 years experience in Python, FastAPI, Docker, and PostgreSQL. "
        "Holds a Bachelor of Science in Computer Science."
    )
    skills = ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "Redis"]  # 3 of 5 matched (60%)
    
    score = recruitment_agent.score_candidate(
        resume_text=resume,
        required_skills=skills,
        required_experience_years=4.0,
        candidate_experience_years=5.0,  # 100% experience score
    )

    assert score.skills_score == 60.0
    assert score.experience_score == 100.0
    assert score.education_score == 90.0
    # Composite: (60 * 0.6) + (100 * 0.3) + (90 * 0.1) = 36 + 30 + 9 = 75.0
    assert score.composite_score == 75.0
    assert score.recommendation == "SHORTLIST"
    assert "Python" in score.matched_skills
    assert "Kubernetes" in score.missing_skills


def test_candidate_deduplication(recruitment_agent):
    """Test cross-channel candidate deduplication across email, phone, and LinkedIn."""
    candidates = [
        {
            "id": "CAND-001",
            "full_name": "Priya Sharma",
            "email": "priya.sharma@example.com",
            "phone": "+91 98765 43210",
            "source": "LINKEDIN",
        },
        {
            "id": "CAND-002",
            "full_name": "Priya Sharma",
            "email": "priya.sharma@example.com",  # Duplicate email
            "phone": "9876543210",
            "source": "EMPLOYEE_REFERRAL",
        },
        {
            "id": "CAND-003",
            "full_name": "Rahul Verma",
            "email": "rahul.verma@example.com",
            "phone": "+91 91234 56789",
            "source": "CAREERS_PORTAL",
        },
    ]

    merged = recruitment_agent.deduplicate_candidates(candidates)

    assert len(merged) == 2
    priya_profile = [p for p in merged if p.email == "priya.sharma@example.com"][0]
    assert priya_profile.application_count == 2
    assert "LINKEDIN" in priya_profile.merged_source_channels
    assert "EMPLOYEE_REFERRAL" in priya_profile.merged_source_channels
    assert len(priya_profile.raw_ids_merged) == 2
