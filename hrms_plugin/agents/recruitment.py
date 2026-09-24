"""Recruitment Specialist Agent: ATS Scoring, Bias Detection, and Candidate Deduplication."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field


# Gender-coded and exclusionary keywords dictionary
GENDER_BIAS_MAP: Dict[str, str] = {
    "rockstar": "exceptional contributor / skilled specialist",
    "ninja": "domain expert / proficient engineer",
    "guru": "subject matter expert / specialist",
    "aggressive": "driven / ambitious / proactive",
    "dominant": "influential / decisive",
    "work hard play hard": "collaborative and high-impact environment",
    "manpower": "workforce / personnel / staffing",
    "freshers only": "early career professionals welcome",
}


class AtsScoreBreakdown(BaseModel):
    skills_score: float
    experience_score: float
    education_score: float
    composite_score: float
    recommendation: str  # "SHORTLIST", "REVIEW_BAND", "REJECT"
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    explanation: str


class BiasAuditResult(BaseModel):
    has_bias: bool
    detected_terms: List[Dict[str, str]] = Field(default_factory=list)
    suggested_rewrite: str


class MergedCandidateProfile(BaseModel):
    primary_id: str
    full_name: str
    email: str
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    merged_source_channels: List[str] = Field(default_factory=list)
    application_count: int
    raw_ids_merged: List[str] = Field(default_factory=list)


class RecruitmentAgent:
    """Specialist agent for talent acquisition, ATS scoring, and candidate ingestion."""

    def audit_job_description_bias(self, jd_text: str) -> BiasAuditResult:
        """Audit job description for exclusionary, gender-biased, or aggressive keywords."""
        detected = []
        lowered = jd_text.lower()
        rewritten = jd_text

        for term, replacement in GENDER_BIAS_MAP.items():
            pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
            if pattern.search(lowered):
                detected.append({
                    "flagged_term": term,
                    "replacement_recommendation": replacement,
                })
                rewritten = pattern.sub(f"[{replacement}]", rewritten)

        return BiasAuditResult(
            has_bias=(len(detected) > 0),
            detected_terms=detected,
            suggested_rewrite=rewritten,
        )

    def score_candidate(
        self,
        resume_text: str,
        required_skills: List[str],
        required_experience_years: float,
        candidate_experience_years: float,
        weights: Optional[Dict[str, float]] = None,
    ) -> AtsScoreBreakdown:
        """Calculate explainable, weighted ATS score for a candidate resume against job criteria."""
        w = weights or {"skills": 0.60, "experience": 0.30, "education": 0.10}

        # Normalize skills
        matched_skills: List[str] = []
        missing_skills: List[str] = []
        lowered_resume = resume_text.lower()

        for skill in required_skills:
            if skill.lower() in lowered_resume:
                matched_skills.append(skill)
            else:
                missing_skills.append(skill)

        # 1. Skills Score (0 - 100)
        skills_score = (
            (len(matched_skills) / len(required_skills) * 100.0)
            if required_skills
            else 100.0
        )

        # 2. Experience Score (0 - 100)
        if required_experience_years <= 0:
            exp_score = 100.0
        else:
            exp_ratio = candidate_experience_years / required_experience_years
            exp_score = min(100.0, exp_ratio * 100.0)

        # 3. Education Score (Default 85 baseline if degree mentioned)
        has_degree = any(deg in lowered_resume for deg in ["bachelor", "master", "b.tech", "m.tech", "bs", "ms", "phd"])
        edu_score = 90.0 if has_degree else 70.0

        # Composite Score
        composite = (
            (skills_score * w.get("skills", 0.60))
            + (exp_score * w.get("experience", 0.30))
            + (edu_score * w.get("education", 0.10))
        )
        composite = round(composite, 2)

        if composite >= 75.0:
            rec = "SHORTLIST"
        elif composite >= 55.0:
            rec = "REVIEW_BAND"
        else:
            rec = "REJECT"

        explanation = (
            f"Candidate scored {composite:.1f}/100. Skills match: {len(matched_skills)}/{len(required_skills)} "
            f"({skills_score:.1f}%). Experience: {candidate_experience_years} yrs vs {required_experience_years} yrs required ({exp_score:.1f}%)."
        )

        return AtsScoreBreakdown(
            skills_score=round(skills_score, 2),
            experience_score=round(exp_score, 2),
            education_score=round(edu_score, 2),
            composite_score=composite,
            recommendation=rec,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            explanation=explanation,
        )

    def deduplicate_candidates(
        self,
        candidates: List[Dict[str, Any]],
    ) -> List[MergedCandidateProfile]:
        """Deduplicate candidates across emails, phone numbers, and LinkedIn profiles with source history merge."""
        merged_map: Dict[str, MergedCandidateProfile] = {}
        index_by_key: Dict[str, str] = {}  # key -> primary_email

        def _clean_phone(p: Optional[str]) -> str:
            return re.sub(r"\D", "", p or "")

        def _clean_url(u: Optional[str]) -> str:
            return (u or "").lower().rstrip("/")

        for cand in candidates:
            c_id = str(cand.get("id") or cand.get("candidate_id") or "CAND-0")
            name = str(cand.get("full_name") or f"{cand.get('first_name', '')} {cand.get('last_name', '')}").strip()
            email = str(cand.get("email") or "").strip().lower()
            phone = _clean_phone(cand.get("phone"))
            linkedin = _clean_url(cand.get("linkedin_url") or cand.get("linkedin"))
            source = str(cand.get("source") or cand.get("channel") or "DIRECT_PORTAL")

            # Check if matching key already exists
            match_key = None
            if email and email in index_by_key:
                match_key = index_by_key[email]
            elif phone and phone in index_by_key:
                match_key = index_by_key[phone]
            elif linkedin and linkedin in index_by_key:
                match_key = index_by_key[linkedin]

            if match_key and match_key in merged_map:
                # Merge into existing profile
                profile = merged_map[match_key]
                if source not in profile.merged_source_channels:
                    profile.merged_source_channels.append(source)
                profile.application_count += 1
                if c_id not in profile.raw_ids_merged:
                    profile.raw_ids_merged.append(c_id)
                # Fill missing details
                if not profile.phone and phone:
                    profile.phone = phone
                if not profile.linkedin_url and linkedin:
                    profile.linkedin_url = linkedin
            else:
                # New unique candidate profile
                primary_key = email or phone or linkedin or c_id
                new_profile = MergedCandidateProfile(
                    primary_id=c_id,
                    full_name=name,
                    email=email,
                    phone=phone if phone else None,
                    linkedin_url=linkedin if linkedin else None,
                    merged_source_channels=[source],
                    application_count=1,
                    raw_ids_merged=[c_id],
                )
                merged_map[primary_key] = new_profile
                if email:
                    index_by_key[email] = primary_key
                if phone:
                    index_by_key[phone] = primary_key
                if linkedin:
                    index_by_key[linkedin] = primary_key

        return list(merged_map.values())
