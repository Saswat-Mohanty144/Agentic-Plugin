"""Cognitive Multi-Agent Fleet for HRMS Domain Specialization."""

from hrms_plugin.agents.compliance import (
    ComplianceAgent,
    ComplianceAuditReport,
    ComplianceViolation,
    ViolationSeverity,
)
from hrms_plugin.agents.leave_attendance import (
    LeaveAttendanceAgent,
    LeaveDeductionResult,
    TravelAnomaly,
)
from hrms_plugin.agents.recruitment import (
    AtsScoreBreakdown,
    BiasAuditResult,
    MergedCandidateProfile,
    RecruitmentAgent,
)
from hrms_plugin.agents.statutory_payroll import (
    IndiaSalaryStructure,
    StatutoryPayrollAgent,
    UaeEosbCalculation,
    UaeSalaryStructure,
)
from hrms_plugin.agents.supervisor import (
    AgentResponse,
    SupervisorAgent,
    UserIntent,
)

__all__ = [
    "ComplianceAgent",
    "ComplianceAuditReport",
    "ComplianceViolation",
    "ViolationSeverity",
    "LeaveAttendanceAgent",
    "LeaveDeductionResult",
    "TravelAnomaly",
    "AtsScoreBreakdown",
    "BiasAuditResult",
    "MergedCandidateProfile",
    "RecruitmentAgent",
    "IndiaSalaryStructure",
    "StatutoryPayrollAgent",
    "UaeEosbCalculation",
    "UaeSalaryStructure",
    "AgentResponse",
    "SupervisorAgent",
    "UserIntent",
]
