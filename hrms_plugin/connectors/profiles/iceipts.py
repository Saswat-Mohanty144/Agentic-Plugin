"""Host profile for iCeipts HRMS (iceipts_hrms).

Handles iCeipts-specific routing conventions, nested requisition envelopes,
typo preservation (e.g. recuiterId), enum mapping, and JWT recruiter extraction.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_JOB_MODE_MAP = {
    "remote": "WorkFromHome",
    "workfromhome": "WorkFromHome",
    "work_from_home": "WorkFromHome",
    "onsite": "WorkFromOffice",
    "office": "WorkFromOffice",
    "workfromoffice": "WorkFromOffice",
    "work_from_office": "WorkFromOffice",
    "hybrid": "Hybrid",
}

_EMPLOYMENT_TYPE_MAP = {
    "full_time": "Full-time",
    "fulltime": "Full-time",
    "part_time": "Part-time",
    "contract": "Contract",
    "temporary": "Temporary",
    "intern": "Internship",
    "internship": "Internship",
}


def extract_jwt_user_id(token: Optional[str]) -> Optional[str]:
    """Extract user_id / cid from an HRMS JWT payload without signature validation."""
    if not token or "." not in token:
        return None
    try:
        raw_token = token[7:] if token.startswith("Bearer ") else token
        parts = raw_token.split(".")
        if len(parts) < 2:
            return None
        payload_part = parts[1]
        padding = "=" * (-len(payload_part) % 4)
        decoded = base64.urlsafe_b64decode(payload_part + padding)
        payload = json.loads(decoded)
        return payload.get("id") or payload.get("cid") or payload.get("userId") or payload.get("user_id")
    except Exception as e:
        logger.debug("[iceipts_profile] failed to decode JWT payload: %s", e)
        return None


class IceiptsProfile(VendorProfile):
    """Production vendor profile for the iCeipts HRMS host application."""

    vendor_name: str = "iceipts"

    def __init__(
        self,
        frontend_origin: str = "http://localhost:3000",
        default_user_id: Optional[str] = None,
    ) -> None:
        self.frontend_origin = frontend_origin
        self.default_user_id = default_user_id or "00000000-0000-0000-0000-000000000000"

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        act = action.lower()

        if entity_type == EntityType.REQUISITION:
            if act in ("create", "post"):
                user_id = kwargs.get("user_id") or extract_jwt_user_id(auth_token) or self.default_user_id
                return f"/job/createRequisition/{user_id}"
            elif act in ("get", "fetch"):
                if not entity_id:
                    raise ValueError("Fetching an iCeipts requisition requires entity_id (job_id)")
                return f"/job/detail/{entity_id}"
            elif act in ("list", "search"):
                return "/job/list"

        elif entity_type == EntityType.EMPLOYEE:
            if act in ("get", "fetch"):
                if not entity_id:
                    raise ValueError("Fetching an iCeipts employee requires entity_id")
                return f"/employee/profile/{entity_id}"
            elif act in ("list", "search"):
                return "/employee/list"
            elif act in ("update", "patch"):
                if not entity_id:
                    raise ValueError("Updating an iCeipts employee requires entity_id")
                return f"/employee/{entity_id}"

        elif entity_type == EntityType.LEAVE_REQUEST:
            if act in ("create", "apply"):
                return "/leave/apply"
            elif act in ("get", "fetch"):
                if not entity_id:
                    raise ValueError("Fetching an iCeipts leave request requires entity_id")
                return f"/leave/detail/{entity_id}"
            elif act in ("list", "search"):
                return "/leave/list"
            elif act in ("update", "status"):
                if not entity_id:
                    raise ValueError("Updating an iCeipts leave status requires entity_id")
                return f"/leave/status/{entity_id}"

        elif entity_type == EntityType.CANDIDATE:
            if act in ("get", "fetch"):
                if not entity_id:
                    raise ValueError("Fetching an iCeipts candidate requires entity_id")
                return f"/candidate/detail/{entity_id}"
            elif act in ("list", "search"):
                return "/candidate/list"

        elif entity_type == EntityType.PUNCH:
            if act in ("create", "punch"):
                return "/attendance/punch"
            elif act in ("list", "history"):
                return "/attendance/history"

        # Fallback to standard conventions
        plural = entity_type.value.lower() + "s"
        return f"/{plural}/{entity_id}" if entity_id and act in ("get", "update", "delete") else f"/{plural}"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": self.frontend_origin,
            "Referer": self.frontend_origin.rstrip("/") + "/",
            "User-Agent": "Mozilla/5.0 (compatible; hrms-agentic-plugin/1.0; +iceipts-profile)",
        }
        if auth_token:
            auth_val = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
            headers["Authorization"] = auth_val
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def unwrap_response(
        self,
        response_data: Any,
        action: str,
        entity_type: EntityType,
    ) -> Any:
        if not isinstance(response_data, dict):
            return response_data

        data = response_data.get("data")
        if isinstance(data, dict):
            # iCeipts job responses wrap job details inside data.job
            if entity_type == EntityType.REQUISITION and "job" in data:
                return data["job"]
            # List endpoints wrap lists in data.rows or data.items
            if "rows" in data and isinstance(data["rows"], list):
                return data["rows"]
            if "items" in data and isinstance(data["items"], list):
                return data["items"]
            return data

        if isinstance(data, list):
            return data

        # If data is None or missing, return the root dictionary
        return response_data

    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if entity_type != EntityType.REQUISITION or action.lower() not in ("create", "post"):
            return canonical_payload

        # Transform CanonicalRequisition into iCeipts wire payload
        user_id = (
            canonical_payload.get("recruiter_id")
            or canonical_payload.get("hiring_manager_id")
            or extract_jwt_user_id(auth_token)
            or self.default_user_id
        )

        title = canonical_payload.get("title") or "Unspecified Role"
        department = canonical_payload.get("department") or "Engineering"
        location = canonical_payload.get("location") or "Remote"

        raw_mode = str(canonical_payload.get("work_model") or canonical_payload.get("job_mode") or "remote").lower()
        job_mode = _JOB_MODE_MAP.get(raw_mode, "WorkFromHome")

        raw_type = str(canonical_payload.get("employment_type") or "full_time").lower()
        emp_type = _EMPLOYMENT_TYPE_MAP.get(raw_type, "Full-time")

        min_sal = canonical_payload.get("min_salary") or canonical_payload.get("salary_min") or 0
        max_sal = canonical_payload.get("max_salary") or canonical_payload.get("salary_max") or 0
        salary_str = f"{min_sal}-{max_sal} LPA" if (min_sal or max_sal) else "Negotiable"

        skills = canonical_payload.get("skills") or canonical_payload.get("required_skills") or []
        skills_str = ", ".join(str(s) for s in skills if s) if isinstance(skills, list) else str(skills)

        desc = (
            canonical_payload.get("description")
            or canonical_payload.get("job_description")
            or f"{title} role in {department}."
        )
        reqs = canonical_payload.get("requirements") or skills_str or "To be discussed during screening."
        resps = canonical_payload.get("responsibilities") or desc

        pos_count = canonical_payload.get("headcount") or canonical_payload.get("position") or 1
        try:
            position_num = int(pos_count)
        except (ValueError, TypeError):
            position_num = 1

        job_data: Dict[str, Any] = {
            "employeeId": user_id,
            "title": title,
            "department": department,
            "location": location,
            "type": emp_type,
            "jobMode": job_mode,
            "shifts": "Morning",
            "description": desc,
            "education": "Any",
            "salary": salary_str,
            "minSalary": int(min_sal) if min_sal else 0,
            "maxSalary": int(max_sal) if max_sal else 0,
            "position": position_num,
            "minthreshold": 70,
            "matchthreshold": 85,
            "jobDescription": desc,
            "requirements": reqs,
            "responsibilities": resps,
            "recuiterId": user_id,  # iCeipts intentional backend typo
            "managerId": user_id,
        }

        # Department UUID handling if passed in metadata or resolved
        dept_id = canonical_payload.get("department_id")
        if dept_id and _UUID_RE.match(str(dept_id)):
            job_data["departmentId"] = str(dept_id)

        valid_skills = [str(s) for s in skills if _UUID_RE.match(str(s))]
        raw_benefits = canonical_payload.get("benefits") or []
        valid_benefits = [str(b) for b in raw_benefits if _UUID_RE.match(str(b))]

        return {
            "employeeId": user_id,
            "jobData": job_data,
            "skills": valid_skills,
            "benefits": valid_benefits,
        }
