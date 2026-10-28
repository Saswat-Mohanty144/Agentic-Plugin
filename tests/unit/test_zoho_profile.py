"""Unit tests for ZohoProfile in hrms-agentic-plugin."""

from __future__ import annotations

from hrms_plugin.connectors.profiles.zoho import ZohoProfile
from hrms_plugin.schema.canonical import EntityType


class TestZohoProfile:
    def setup_method(self):
        self.profile = ZohoProfile(default_department="Engineering")

    def test_endpoint_routing(self):
        # Employee
        assert self.profile.endpoint_for(EntityType.EMPLOYEE, "list") == "/forms/json/employee/getRecords"
        assert (
            self.profile.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="1001")
            == "/forms/json/employee/getRecordByID?recordId=1001"
        )
        assert self.profile.endpoint_for(EntityType.EMPLOYEE, "create") == "/forms/json/employee/insertRecord"
        assert (
            self.profile.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="1001")
            == "/forms/json/employee/updateRecord?recordId=1001"
        )

        # Leave
        assert self.profile.endpoint_for(EntityType.LEAVE_REQUEST, "create") == "/forms/json/leave/insertRecord"
        assert self.profile.endpoint_for(EntityType.LEAVE_BALANCE, "list") == "/forms/json/leave/getEmployeeLeaveTypes"

        # Punch
        assert self.profile.endpoint_for(EntityType.PUNCH, "create") == "/attendance/punchIn"
        assert self.profile.endpoint_for(EntityType.PUNCH, "list") == "/attendance/getUserAttendance"

        # Requisition
        assert self.profile.endpoint_for(EntityType.REQUISITION, "create") == "/forms/json/jobopening/insertRecord"

    def test_headers_preparation(self):
        headers = self.profile.prepare_headers(auth_token="1000.abc123xyz")
        assert headers["Authorization"] == "Zoho-oauthtoken 1000.abc123xyz"
        assert headers["Content-Type"] == "application/json"

        # Already prefixed token
        headers_prefixed = self.profile.prepare_headers(auth_token="Zoho-oauthtoken 1000.abc123xyz")
        assert headers_prefixed["Authorization"] == "Zoho-oauthtoken 1000.abc123xyz"

    def test_unwrap_response(self):
        # Standard Zoho People envelope
        payload = {
            "response": {
                "result": [
                    {"EmployeeID": "EMP-01", "FirstName": "John"},
                    {"EmployeeID": "EMP-02", "FirstName": "Jane"},
                ]
            }
        }
        unwrapped = self.profile.unwrap_response(payload, "list", EntityType.EMPLOYEE)
        assert isinstance(unwrapped, list)
        assert len(unwrapped) == 2
        assert unwrapped[0]["EmployeeID"] == "EMP-01"

    def test_normalize_payload(self):
        canonical = {
            "full_name": "Aarav Sharma",
            "work_email": "aarav@company.com",
            "phone": "+919876543210",
            "joining_date": "2026-04-01",
        }
        wire = self.profile.normalize_payload_for_host(canonical, EntityType.EMPLOYEE, "create")
        assert wire["FirstName"] == "Aarav"
        assert wire["LastName"] == "Sharma"
        assert wire["EmailID"] == "aarav@company.com"
        assert wire["Mobile"] == "+919876543210"
        assert wire["Dateofjoining"] == "2026-04-01"
        assert wire["Department"] == "Engineering"

    def test_heal_payload_from_error(self):
        raw_wire = {"FirstName": "Aarav"}
        error_msg = "Error 7001: Mandatory field 'Department' missing"
        healed = self.profile.heal_payload_from_error(raw_wire, EntityType.EMPLOYEE, error_msg)
        assert healed is not None
        assert healed["Department"] == "Engineering"
