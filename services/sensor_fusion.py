"""Graduated autonomous speed limits using the live safety sensor snapshot.

This is a pre-filter only. Every resulting command still passes through the
central SafetyLayer, which remains authoritative for e-stop and hard stops.
"""
from __future__ import annotations

import os
import threading
import time

CAUTION_M = float(os.getenv("SENTRA_FUSION_CAUTION_M", "0.8"))
SLOW_M = float(os.getenv("SENTRA_FUSION_SLOW_M", "0.4"))
STOP_M = float(os.getenv("SENTRA_FUSION_STOP_M", "0.2"))
_lock = threading.RLock()
_last = {"tier": "CLEAR", "reason": "clear", "updated_at": None}


def _inputs() -> tuple[dict, dict | None, dict | None]:
    from core.safety import get_safety_layer
    from services import apriltag_service, person_detection
    sensors = get_safety_layer().read_sensors()
    person = next((p for p in person_detection.get_tracked()
                   if p.get("confidence", 0) > 0.6 and p.get("age_s", 99) < 1.0), None)
    tag = None
    try:
        candidates = apriltag_service.get_recent(1)
        candidate = candidates[0] if candidates else None
        if candidate and time.time() - float(candidate.get("timestamp", 0)) <= 2.0:
            tag = candidate
    except (AttributeError, IndexError):
        pass
    return sensors, person, tag


def get_speed_decision(requested_left: float, requested_right: float) -> tuple[float, float, str, str]:
    """Return capped wheel speeds, active tier, and an explainable reason."""
    sensors, person, tag = _inputs()
    front = sensors.get("front_distance_m")
    left_cliff = bool(sensors.get("left_cliff"))
    right_cliff = bool(sensors.get("right_cliff"))
    scale, tier, reasons = 1.0, "CLEAR", []
    if isinstance(front, (int, float)):
        if front < STOP_M:
            scale, tier = 0.0, "STOP"
            reasons.append(f"front obstacle {front:.2f}m")
        elif front < SLOW_M:
            scale, tier = min(scale, 0.2), "SLOW"
            reasons.append(f"front obstacle {front:.2f}m")
        elif front < CAUTION_M:
            scale, tier = min(scale, 0.5), "CAUTION"
            reasons.append(f"front obstacle {front:.2f}m")
    if person:
        pscale = 0.1 if tier == "SLOW" else 0.4
        if pscale < scale:
            scale = pscale
            tier = "SLOW" if pscale <= 0.2 else "CAUTION"
        reasons.append("person detected")
    left, right = requested_left * scale, requested_right * scale
    if left_cliff and right_cliff:
        left = right = 0.0
        tier = "STOP"
        reasons.append("both cliff sensors triggered")
    elif left_cliff or right_cliff:
        left *= 0.5
        right *= 0.5
        # Turn away from the detected edge while preserving the central gate.
        if left_cliff:
            left, right = min(left, 0.0), max(right, 0.0)
            reasons.append("left cliff; steering right")
        else:
            left, right = max(left, 0.0), min(right, 0.0)
            reasons.append("right cliff; steering left")
        if tier == "CLEAR":
            tier = "SLOW"
    if tag and abs(float(tag.get("bearing_deg") or 0.0)) > 20:
        left, right = left * 0.8, right * 0.8
        reasons.append("AprilTag bearing drift")
        if tier == "CLEAR":
            tier = "CAUTION"
    reason = "; ".join(reasons) if reasons else "clear"
    with _lock:
        _last.update(tier=tier, reason=reason, updated_at=time.time())
    return left, right, tier, reason


def get_fusion_status() -> dict:
    with _lock:
        return dict(_last)
