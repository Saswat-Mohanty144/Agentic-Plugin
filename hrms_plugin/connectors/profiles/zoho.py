"""Host profile for Zoho People & Zoho Recruit HRMS suites.

Supports Zoho People JSON Form APIs, token authentication ('Zoho-oauthtoken'),
Zoho response envelope unwrapping, and field normalization.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.schema.canonical import EntityType

logger = logging.getLogger(__name__)


class ZohoProfile(VendorProfile):
    """Vendor profile for Zoho People / Zoho Recruit platforms."""

    vendor_name: str = "zoho"

    def __init__(
        self,
        default_headers: Optional[Dict[str, str]] = None,
        default_department: str = "General",
    ) -> None:
        self.default_headers = default_headers or {}
        self.default_department = default_department

    def endpoint_for(
        self,
        entity_type: EntityType,
        action: str,
        entity_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        act = action.lower()

        # Map entity type to Zoho form name
        form_map = {
            EntityType.EMPLOYEE: "employee",
            EntityType.LEAVE_REQUEST: "leave",
            EntityType.LEAVE_BALANCE: "leave",
            EntityType.REQUISITION: "jobopening",
            EntityType.CANDIDATE: "candidate",
            EntityType.PAYSLIP: "salary",
            EntityType.EXPENSE_CLAIM: "expense",
            EntityType.ASSET: "asset",
        }

        form_name = form_map.get(entity_type, entity_type.value.lower())

        # Special routing for Attendance / Punch
        if entity_type == EntityType.PUNCH:
            if act in ("post", "create", "punch_in"):
                return "/attendance/punchIn"
            elif act in ("punch_out",):
                return "/attendance/punchOut"
            return "/attendance/getUserAttendance"

        # Special routing for Leave Balances
        if entity_type == EntityType.LEAVE_BALANCE:
            return "/forms/json/leave/getEmployeeLeaveTypes"

        # Standard Zoho People Form JSON endpoints
        if act in ("get", "fetch"):
            if entity_id:
                return f"/forms/json/{form_name}/getRecordByID?recordId={entity_id}"
            return f"/forms/json/{form_name}/getRecords"
        elif act in ("list", "read"):
            return f"/forms/json/{form_name}/getRecords"
        elif act in ("create", "post", "add"):
            return f"/forms/json/{form_name}/insertRecord"
        elif act in ("update", "patch", "put"):
            if entity_id:
                return f"/forms/json/{form_name}/updateRecord?recordId={entity_id}"
            return f"/forms/json/{form_name}/updateRecord"
        elif act in ("delete",):
            if entity_id:
                return f"/forms/json/{form_name}/deleteRecord?recordId={entity_id}"
            return f"/forms/json/{form_name}/deleteRecord"

        return f"/forms/json/{form_name}/getRecords"

    def prepare_headers(
        self,
        auth_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hrms-agentic-plugin/2.0 (+zoho-profile)",
        }
        if auth_token:
            if auth_token.startswith("Zoho-oauthtoken ") or auth_token.startswith("Bearer "):
                headers["Authorization"] = auth_token
            else:
                headers["Authorization"] = f"Zoho-oauthtoken {auth_token}"
        headers.update(self.default_headers)
        if custom_headers:
            headers.update(custom_headers)
        return headers

    def unwrap_response(
        self,
        response_data: Any,
        action: str,
        entity_type: EntityType,
    ) -> Any:
        """Unwrap Zoho People response payloads from {'response': {'result': [...]}}."""
        if not isinstance(response_data, dict):
            return response_data

        # Zoho standard envelope: {"response": {"result": [...]}}
        if "response" in response_data and isinstance(response_data["response"], dict):
            resp_inner = response_data["response"]
            if "result" in resp_inner:
                res = resp_inner["result"]
                # If result is wrapped inside a single key like {"result": [{"EmployeeID": ...}]}
                if isinstance(res, list) and len(res) == 1 and isinstance(res[0], dict) and len(res[0]) == 1:
                    first_key = next(iter(res[0]))
                    if isinstance(res[0][first_key], (list, dict)):
                        return res[0][first_key]
                return res
            if "data" in resp_inner:
                return resp_inner["data"]

        if "data" in response_data:
            return response_data["data"]

        return response_data

    def normalize_payload_for_host(
        self,
        canonical_payload: Dict[str, Any],
        entity_type: EntityType,
        action: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert canonical payload fields into Zoho People form field naming conventions."""
        payload = dict(canonical_payload)

        if entity_type == EntityType.EMPLOYEE:
            if "full_name" in payload:
                parts = str(payload["full_name"]).split(maxsplit=1)
                payload.setdefault("FirstName", parts[0])
                if len(parts) > 1:
                    payload.setdefault("LastName", parts[1])
                else:
                    payload.setdefault("LastName", parts[0])
            if "first_name" in payload:
                payload["FirstName"] = payload["first_name"]
            if "last_name" in payload:
                payload["LastName"] = payload["last_name"]
            if "work_email" in payload:
                payload["EmailID"] = payload["work_email"]
            if "phone" in payload:
                payload["Mobile"] = payload["phone"]
            if "department" in payload:
                payload["Department"] = payload["department"]
            else:
                payload.setdefault("Department", self.default_department)
            if "designation" in payload:
                payload["Designation"] = payload["designation"]
            if "joining_date" in payload:
                payload["Dateofjoining"] = str(payload["joining_date"])

        elif entity_type == EntityType.LEAVE_REQUEST:
            if "leave_type" in payload:
                payload["LeaveType"] = str(payload["leave_type"]).title()
            if "start_date" in payload:
                payload["From"] = str(payload["start_date"])
            if "end_date" in payload:
                payload["To"] = str(payload["end_date"])
            if "reason" in payload:
                payload["Reasonforleave"] = payload["reason"]
            if "employee_ref" in payload:
                payload["Employee_ID"] = payload["employee_ref"]

        elif entity_type == EntityType.REQUISITION:
            if "title" in payload:
                payload["JobTitle"] = payload["title"]
            if "department" in payload:
                payload["Department"] = payload["department"]
            else:
                payload.setdefault("Department", self.default_department)
            if "description" in payload:
                payload["JobDescription"] = payload["description"]

        elif entity_type == EntityType.PUNCH:
            if "employee_ref" in payload:
                payload["empId"] = payload["employee_ref"]
            if "punch_time" in payload:
                payload["punchTime"] = str(payload["punch_time"])

        return payload

    def extract_error_message(self, response_body: Any, default: str) -> str:
        """Extract user-friendly validation error message from Zoho response."""
        if isinstance(response_body, dict):
            if "response" in response_body and isinstance(response_body["response"], dict):
                resp = response_body["response"]
                if "errors" in resp:
                    errors = resp["errors"]
                    if isinstance(errors, dict) and "message" in errors:
                        return str(errors["message"])
                    return str(errors)
                if "message" in resp:
                    return str(resp["message"])
            if "message" in response_body:
                return str(response_body["message"])
        return default

    def heal_payload_from_error(
        self,
        payload: Dict[str, Any],
        entity_type: EntityType,
        error_msg: str,
    ) -> Optional[Dict[str, Any]]:
        """Autonomous self-healing: patch missing required Zoho attributes if detected in error."""
        healed = dict(payload)
        modified = False
        err_lower = error_msg.lower()

        if "department" in err_lower and "Department" not in healed:
            healed["Department"] = self.default_department
            modified = True

        if "lastname" in err_lower and "LastName" not in healed:
            if "FirstName" in healed:
                healed["LastName"] = healed["FirstName"]
                modified = True

        return healed if modified else None
