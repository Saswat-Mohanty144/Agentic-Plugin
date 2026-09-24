"""Leave and Attendance Specialist Agent: Deductions, Policies & Geolocation Anomaly Detection."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two geographic coordinates in kilometers."""
    r = 6371.0  # Earth's mean radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


class LeaveDeductionResult(BaseModel):
    employee_id: str
    leave_type: str
    requested_days: float
    current_balance: float
    remaining_balance: float
    is_approved: bool
    rejection_reason: Optional[str] = None
    requires_manager_override: bool = False


class TravelAnomaly(BaseModel):
    is_anomaly: bool
    speed_kmh: float
    distance_km: float
    time_delta_hours: float
    explanation: str
    first_punch_time: str
    second_punch_time: str


class LeaveAttendanceAgent:
    """Specialist agent for leave management and attendance anomaly detection."""

    def evaluate_leave_request(
        self,
        employee_id: str,
        leave_type: str,
        requested_days: float,
        current_balance: float,
        is_on_probation: bool = False,
        allow_negative_balance: bool = False,
    ) -> LeaveDeductionResult:
        """Evaluate a leave application against balances and employment status."""
        if requested_days <= 0:
            return LeaveDeductionResult(
                employee_id=employee_id,
                leave_type=leave_type,
                requested_days=requested_days,
                current_balance=current_balance,
                remaining_balance=current_balance,
                is_approved=False,
                rejection_reason="Requested leave days must be greater than 0.",
            )

        # Check probation policy (e.g. casual leave restricted during probation)
        if is_on_probation and leave_type.upper() in ["ANNUAL", "PRIVILEGE", "CASUAL"]:
            # Check if balance is sufficient, but flag manager override
            if current_balance < requested_days:
                return LeaveDeductionResult(
                    employee_id=employee_id,
                    leave_type=leave_type,
                    requested_days=requested_days,
                    current_balance=current_balance,
                    remaining_balance=current_balance,
                    is_approved=False,
                    rejection_reason="Insufficient balance and employee is currently in probationary status.",
                    requires_manager_override=True,
                )

        if current_balance >= requested_days:
            return LeaveDeductionResult(
                employee_id=employee_id,
                leave_type=leave_type,
                requested_days=requested_days,
                current_balance=current_balance,
                remaining_balance=round(current_balance - requested_days, 2),
                is_approved=True,
            )
        elif allow_negative_balance:
            return LeaveDeductionResult(
                employee_id=employee_id,
                leave_type=leave_type,
                requested_days=requested_days,
                current_balance=current_balance,
                remaining_balance=round(current_balance - requested_days, 2),
                is_approved=True,
                requires_manager_override=True,
            )
        else:
            reason = (
                f"Insufficient balance: requested {requested_days} days "
                f"but only {current_balance} days available."
            )
            return LeaveDeductionResult(
                employee_id=employee_id,
                leave_type=leave_type,
                requested_days=requested_days,
                current_balance=current_balance,
                remaining_balance=current_balance,
                is_approved=False,
                rejection_reason=reason,
            )

    def detect_impossible_travel(
        self,
        punch1: Dict[str, Any],
        punch2: Dict[str, Any],
        max_realistic_speed_kmh: float = 850.0,
    ) -> TravelAnomaly:
        """Analyze two successive check-in punch coordinates to detect impossible physical travel."""
        lat1 = float(punch1.get("latitude") or punch1.get("lat") or 0.0)
        lon1 = float(punch1.get("longitude") or punch1.get("lng") or 0.0)
        t1_str = str(punch1.get("timestamp") or punch1.get("created_at"))

        lat2 = float(punch2.get("latitude") or punch2.get("lat") or 0.0)
        lon2 = float(punch2.get("longitude") or punch2.get("lng") or 0.0)
        t2_str = str(punch2.get("timestamp") or punch2.get("created_at"))

        try:
            dt1 = datetime.fromisoformat(t1_str.replace("Z", "+00:00"))
            dt2 = datetime.fromisoformat(t2_str.replace("Z", "+00:00"))
        except Exception:
            return TravelAnomaly(
                is_anomaly=False,
                speed_kmh=0.0,
                distance_km=0.0,
                time_delta_hours=0.0,
                explanation="Could not parse punch timestamps.",
                first_punch_time=t1_str,
                second_punch_time=t2_str,
            )

        delta_seconds = abs((dt2 - dt1).total_seconds())
        delta_hours = delta_seconds / 3600.0

        distance_km = _haversine_km(lat1, lon1, lat2, lon2)

        if delta_hours == 0:
            speed = 999999.0 if distance_km > 0.1 else 0.0
        else:
            speed = distance_km / delta_hours

        is_anomaly = bool(distance_km > 5.0 and speed > max_realistic_speed_kmh)

        if is_anomaly:
            explanation = (
                f"Impossible travel detected: {distance_km:.1f} km traversed in {delta_hours:.2f} hours "
                f"({speed:.1f} km/h), exceeding maximum realistic transit threshold of {max_realistic_speed_kmh} km/h."
            )
        else:
            explanation = (
                f"Travel speed of {speed:.1f} km/h across {distance_km:.1f} km "
                "is within physically plausible parameters."
            )

        return TravelAnomaly(
            is_anomaly=is_anomaly,
            speed_kmh=round(speed, 2),
            distance_km=round(distance_km, 2),
            time_delta_hours=round(delta_hours, 2),
            explanation=explanation,
            first_punch_time=t1_str,
            second_punch_time=t2_str,
        )
