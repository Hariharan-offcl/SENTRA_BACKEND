"""Coherent demo-facing state snapshots; keeps existing state modules intact."""

from __future__ import annotations

import time


def snapshot() -> dict:
    from core.state import get_robot_state
    from services import (navigation_service, patrol_service, fall_detection,
                          person_recognition, emergency_call)

    rs = get_robot_state()
    now = time.time()
    return {
        "operation": {
            "mode": rs.get_mode(),
            "mode_owner": rs.get_mode_owner(),
            "estop_active": rs.is_estop_active(),
            "motors_disabled": rs.are_motors_disabled(),
            "moving": rs.is_moving(),
        },
        "mission": patrol_service.status(),
        "navigation": navigation_service.status(),
        "detection": {
            "fall": fall_detection.get_status(),
            "recognition": person_recognition.stats(),
        },
        "call": emergency_call.get_status(),
        "timestamp": now,
    }
