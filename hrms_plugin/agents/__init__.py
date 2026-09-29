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
from hrms_plugin.agents.offboarding import (
    FinalSettlementStatement,
    FnfTimelineAudit,
    OffboardingAgent,
    OffboardingClearance,
)
from hrms_plugin.agents.onboarding import (
    DocumentChecklistResult,
    OfferEvaluationResult,
    OnboardingAgent,
    ProvisioningPlanResult,
)
from hrms_plugin.agents.orchestrator import AgenticOrchestrator
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
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.agents.travel_expense import (
    ExpenseAuditReport,
    ExpenseViolation,
    TravelExpenseAgent,
)
from hrms_plugin.agents.voice import VoiceAgent

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
    "VoiceAgent",
    "AgenticOrchestrator",
    "AgenticToolRegistry",
    "OnboardingAgent",
    "OfferEvaluationResult",
    "DocumentChecklistResult",
    "ProvisioningPlanResult",
    "OffboardingAgent",
    "FnfTimelineAudit",
    "FinalSettlementStatement",
    "OffboardingClearance",
    "TravelExpenseAgent",
    "ExpenseAuditReport",
    "ExpenseViolation",
]
