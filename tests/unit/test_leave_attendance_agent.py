"""Unit tests for Leave and Attendance Specialist Agent."""

import pytest
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent


@pytest.fixture
def leave_agent():
    return LeaveAttendanceAgent()


def test_leave_evaluation_sufficient_balance(leave_agent):
    """Test standard leave approval when balance is sufficient."""
    res = leave_agent.evaluate_leave_request(
        employee_id="EMP-001",
        leave_type="ANNUAL",
        requested_days=3.0,
        current_balance=10.0,
    )
    assert res.is_approved is True
    assert res.remaining_balance == 7.0
    assert res.rejection_reason is None


def test_leave_evaluation_insufficient_balance(leave_agent):
    """Test leave rejection on insufficient balance."""
    res = leave_agent.evaluate_leave_request(
        employee_id="EMP-001",
        leave_type="ANNUAL",
        requested_days=12.0,
        current_balance=5.0,
        allow_negative_balance=False,
    )
    assert res.is_approved is False
    assert res.remaining_balance == 5.0
    assert "Insufficient balance" in res.rejection_reason


def test_impossible_travel_detection(leave_agent):
    """Test impossible travel anomaly detection between Mumbai and Dubai within 1 hour."""
    # Mumbai coordinates
    punch1 = {
        "lat": 19.0760,
        "lng": 72.8777,
        "timestamp": "2026-09-24T09:00:00Z",
    }
    # Dubai coordinates (~1920 km away)
    punch2 = {
        "lat": 25.2048,
        "lng": 55.2708,
        "timestamp": "2026-09-24T10:00:00Z",  # 1 hour later
    }

    res = leave_agent.detect_impossible_travel(punch1, punch2)

    assert res.is_anomaly is True
    assert res.distance_km > 1800.0
    assert res.speed_kmh > 1800.0  # Impossible speed (>850 km/h)
    assert "Impossible travel detected" in res.explanation


def test_realistic_commute_no_anomaly(leave_agent):
    """Test realistic commute distance and speed."""
    punch1 = {
        "lat": 19.0760,
        "lng": 72.8777,
        "timestamp": "2026-09-24T09:00:00Z",
    }
    punch2 = {
        "lat": 19.1136,
        "lng": 72.8697,  # ~4.2 km away
        "timestamp": "2026-09-24T09:30:00Z",  # 30 mins later (~8.4 km/h)
    }

    res = leave_agent.detect_impossible_travel(punch1, punch2)

    assert res.is_anomaly is False
    assert res.speed_kmh < 50.0
