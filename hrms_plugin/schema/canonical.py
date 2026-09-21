"""Universal Canonical Domain Models for HRMS entities.

Agents reason strictly against these canonical models. The models carry only what agents
actually need for perception, legal compliance, and autonomous decisions.
Any unmodelled vendor attributes remain in `raw` for round-tripping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "EntityType",
    "SourceRef",
    "CanonicalCandidate",
    "CanonicalRequisition",
    "CanonicalEmployee",
    "CanonicalLeaveRequest",
    "CanonicalLeaveBalance",
    "CanonicalPunch",
    "CanonicalAsset",
    "CanonicalError",
]


class CanonicalError(Exception):
    """Raised when an entity cannot be mapped into or out of canonical form."""


class EntityType(str, Enum):
    CANDIDATE = "CANDIDATE"
    REQUISITION = "REQUISITION"
    EMPLOYEE = "EMPLOYEE"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    LEAVE_BALANCE = "LEAVE_BALANCE"
    LEAVE_REQUEST = "LEAVE_REQUEST"
    PUNCH = "PUNCH"
    PAYROLL_RUN = "PAYROLL_RUN"
    PAYSLIP = "PAYSLIP"
    EXPENSE_CLAIM = "EXPENSE_CLAIM"
    ATTENDANCE_REQUEST = "ATTENDANCE_REQUEST"
    APPRAISAL = "APPRAISAL"
    ASSET = "ASSET"


@dataclass(frozen=True)
class SourceRef:
    """Represents where a record lives in the host HRMS system."""

    vendor: str
    entity_type: EntityType
    external_id: str
    version: Optional[str] = None  # Concurrency token (ETag / updated_at)
    url: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.vendor}:{self.entity_type.value}:{self.external_id}"


@dataclass
class CanonicalCandidate:
    source_ref: SourceRef
    tenant_id: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    resume_text: Optional[str] = None
    resume_uri: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    years_experience: Optional[Decimal] = None
    current_title: Optional[str] = None
    education: List[str] = field(default_factory=list)
    stage: Optional[str] = None
    requisition_external_id: Optional[str] = None
    applied_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def pii_literals(self) -> List[str]:
        """Strings the masking gateway must scrub before calling LLMs."""
        literals = []
        if self.full_name:
            literals.extend([p for p in self.full_name.split() if len(p) > 1])
        if self.email:
            literals.append(self.email)
        if self.phone:
            literals.append(self.phone)
        return literals


@dataclass
class CanonicalRequisition:
    source_ref: SourceRef
    tenant_id: str
    title: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    headcount: int = 1
    status: Optional[str] = None
    description: Optional[str] = None
    must_have_skills: List[str] = field(default_factory=list)
    nice_to_have_skills: List[str] = field(default_factory=list)
    min_years_experience: Optional[Decimal] = None
    max_years_experience: Optional[Decimal] = None
    hiring_manager_ref: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalEmployee:
    source_ref: SourceRef
    tenant_id: str
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    work_email: Optional[str] = None
    personal_email: Optional[str] = None
    phone: Optional[str] = None
    joining_date: Optional[date] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    employment_status: Optional[str] = None
    manager_ref: Optional[str] = None
    salary: Optional[Decimal] = None
    currency: Optional[str] = "USD"
    raw: Dict[str, Any] = field(default_factory=dict)

    def pii_literals(self) -> List[str]:
        literals = []
        if self.full_name:
            literals.extend([p for p in self.full_name.split() if len(p) > 1])
        if self.work_email:
            literals.append(self.work_email)
        if self.personal_email:
            literals.append(self.personal_email)
        if self.phone:
            literals.append(self.phone)
        return literals


@dataclass
class CanonicalLeaveRequest:
    source_ref: SourceRef
    tenant_id: str
    employee_ref: str
    leave_type: str
    start_date: date
    end_date: date
    total_days: Decimal
    reason: Optional[str] = None
    status: Optional[str] = "PENDING"
    approved_by: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalLeaveBalance:
    source_ref: SourceRef
    tenant_id: str
    employee_ref: str
    leave_type: str
    allocated_days: Decimal
    used_days: Decimal
    remaining_days: Decimal
    fiscal_year: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalPunch:
    source_ref: SourceRef
    tenant_id: str
    employee_ref: str
    punch_time: datetime
    punch_type: str = "IN"  # IN / OUT
    device_id: Optional[str] = None
    location: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalAsset:
    source_ref: SourceRef
    tenant_id: str
    asset_tag: str
    asset_type: str = "LAPTOP"
    model: Optional[str] = None
    serial_number: Optional[str] = None
    assigned_to: Optional[str] = None
    status: str = "IN_USE"
    issued_on: Optional[date] = None
    raw: Dict[str, Any] = field(default_factory=dict)
