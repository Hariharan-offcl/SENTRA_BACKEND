"""
SENTRA — Return-to-dock service (Phase 8).

Tag-guided docking WITHOUT global coordinates (per the project constraint):
the rover uses only what it can see and measure now.

State machine (RETURN_TO_DOCK mode, owner "docking_service"):

    SEEK      Rotate in place until the dock tag appears (timeout → ABORT).
    APPROACH  Drive toward the tag steered by its bearing; slow down inside
              DOCK_SLOW_M; reached when tag distance ≤ DOCK_STOP_M.
              Tag lost > grace window → back to SEEK.
    ALIGN     Rotate until the tag bearing is within ±DOCK_ALIGN_TOL_DEG
              (the rover squares itself with the dock marker).
    DOCKED    Stop. Session ends in STANDBY. DOCK event pushed to /ws/alerts.

Safety:
    - Every duty goes through the safety gate (obstacle/cliff/estop/timeout).
    - Gate refusals or path-blocked → stop and wait briefly (retry), then
      re-evaluate state (a blocked approach may re-seek if the tag is lost).
    - Manual takeover / e-stop / mode change cancels the session cleanly.

The dock tag is whatever tag has type "DOCK" in the tag map (default id 1).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from core import config as core_config
from core.state import get_robot_state, RETURN_TO_DOCK, STANDBY
from services import safety_events

logger = logging.getLogger(__name__)

ENGINE_TICK_S = 0.1
BLOCK_RETRY_WINDOW_S = 3.0     # gate-blocked longer than this in approach → re-seek/abort

_lock = threading.RLock()
_session: Optional[dict] = None
_engine_thread: threading.Thread | None = None
_engine_stop = threading.Event()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_event(detail: dict, severity: str = "INFO") -> None:
    try:
        safety_events.report("DOCK", detail, severity=severity)
    except Exception:
        pass


def dock_tag_id() -> Optional[int]:
    """The tag registered as type DOCK (first one wins)."""
    from services import tag_map
    for t in tag_map.list_tags():
        if t.get("type") == "DOCK":
            return t["tag_id"]
    return None


def _latest_detection(tag_id: int, max_age_s: float) -> Optional[dict]:
    from services import apriltag_service
    det = apriltag_service.get_last_seen(tag_id)
    if det and (time.time() - det["timestamp"]) <= max_age_s:
        return det
    return None


# ── Session management ───────────────────────────────────────────────────────

def start_return(docked_by: str = "rest") -> dict:
    """Begin RETURN_TO_DOCK. Validates a DOCK tag exists; enters mode."""
    global _session
    st = get_robot_state()
    if st.is_estop_active():
        return {"ok": False, "error": "estop_latched"}
    tag_id = dock_tag_id()
    if tag_id is None:
        return {"ok": False, "error": "no_dock_tag_registered"}

    result = st.request_mode(RETURN_TO_DOCK, requested_by="docking_service")
    if not result.get("accepted"):
        return {"ok": False, "error": f"mode_unavailable ({st.get_mode()})"}

    with _lock:
        _session = {
            "id": f"dock-{int(time.time())}",
            "state": "SEEK",
            "started_at": time.time(),
            "state_started_at": time.time(),
            "tag_id": tag_id,
            "blocked_since": None,
            "search_direction": 1,   # +1 rotate right, -1 rotate left
            "docked_by": docked_by,
        }
    logger.info("Return-to-dock session %s started (tag %d)", _session["id"], tag_id)
    _log_event({"event": "started", "tag_id": tag_id})
    return {"ok": True, "session": status()}


def cancel_return(reason: str = "cancelled") -> dict:
    """Cancel any active docking session and stop motors."""
    global _session
    with _lock:
        had = _session is not None
        if _session:
            _session["ended_reason"] = reason
            _session = None
    from services.motor_service import stop_all
    stop_all("dock_cancel")
    st = get_robot_state()
    if st.get_mode() == RETURN_TO_DOCK:
        st.request_mode(STANDBY, requested_by="docking_service")
    if had:
        logger.info("Return-to-dock cancelled (%s)", reason)
        _log_event({"event": "cancelled", "reason": reason})
    return {"ok": True, "was_active": had}


def _end_session_docked() -> None:
    global _session
    with _lock:
        tag_id = _session["tag_id"] if _session else None
        _session = None
    from services.motor_service import stop_all
    stop_all("dock_done")
    st = get_robot_state()
    if st.get_mode() == RETURN_TO_DOCK:
        st.request_mode(STANDBY, requested_by="docking_service")
    logger.info("DOCKED at tag %s", tag_id)
    _log_event({"event": "docked", "tag_id": tag_id}, severity="INFO")


def status() -> Optional[dict]:
    with _lock:
        if _session is None:
            return None
        sess = dict(_session)
        sess["age_s"] = round(time.time() - sess["started_at"], 1)
        return sess


# ── State handlers ───────────────────────────────────────────────────────────

def _set_state(sess: dict, new_state: str) -> None:
    with _lock:
        if _session and _session["id"] == sess["id"]:
            _session["state"] = new_state
            _session["state_started_at"] = time.time()
            _session["blocked_since"] = None
    logger.info("Dock %s: %s → %s", sess["id"], sess["state"], new_state)


def _handle_gate_refusal(sess: dict, decision: dict) -> None:
    """Gate refused (obstacle/timeout/etc.) — stop; track persistent blocks."""
    from services.motor_service import stop_all
    stop_all("dock_gate")
    now = time.time()
    with _lock:
        if _session and _session["id"] == sess["id"]:
            if _session["blocked_since"] is None:
                _session["blocked_since"] = now
            blocked_for = now - _session["blocked_since"]
        else:
            blocked_for = 0.0
    if blocked_for > BLOCK_RETRY_WINDOW_S:
        if sess["state"] == "APPROACH":
            logger.warning("Dock %s: approach blocked >%.0fs — re-seeking",
                           sess["id"], BLOCK_RETRY_WINDOW_S)
            _set_state(sess, "SEEK")


def _state_seek(sess: dict) -> None:
    """Rotate in place until the dock tag is visible."""
    from services.motor_service import apply_wheel_speeds
    now = time.time()
    if now - sess["state_started_at"] > core_config.DOCK_SEARCH_TIMEOUT_S:
        logger.warning("Dock %s: search timed out", sess["id"])
        _log_event({"event": "search_timeout"}, severity="WARNING")
        cancel_return("search_timeout")
        return

    det = _latest_detection(sess["tag_id"], max_age_s=core_config.PATROL_CONFIRM_FRESH_S)
    if det is not None:
        _set_state(sess, "APPROACH")
        return

    duty = core_config.DOCK_SEARCH_TURN_DUTY
    direction = 1 if sess.get("search_direction", 1) > 0 else -1
    decision = apply_wheel_speeds(direction * duty, -direction * duty,
                                  owner="docking_service", mode=RETURN_TO_DOCK)
    if not decision.get("applied"):
        _handle_gate_refusal(sess, decision)


def _state_approach(sess: dict) -> None:
    """Drive toward the tag, steered by bearing; slow near; stop at DOCK_STOP_M."""
    from services.motor_service import apply_wheel_speeds
    now = time.time()
    if now - sess["state_started_at"] > core_config.DOCK_APPROACH_TIMEOUT_S:
        logger.warning("Dock %s: approach timed out", sess["id"])
        _log_event({"event": "approach_timeout"}, severity="WARNING")
        cancel_return("approach_timeout")
        return

    det = _latest_detection(sess["tag_id"], max_age_s=core_config.DOCK_TAG_LOST_GRACE_S)
    if det is None:
        logger.warning("Dock %s: tag lost during approach — re-seeking", sess["id"])
        _set_state(sess, "SEEK")
        return

    distance = det.get("distance_m")
    bearing = det.get("bearing_deg") or 0.0

    if distance is not None and distance <= core_config.DOCK_STOP_M:
        logger.info("Dock %s: reached dock (%.2fm) — aligning", sess["id"], distance)
        _set_state(sess, "ALIGN")
        return

    # Speed: slow down inside DOCK_SLOW_M
    if distance is not None and distance < core_config.DOCK_SLOW_M:
        duty = core_config.DOCK_NEAR_DUTY
    else:
        duty = core_config.DOCK_APPROACH_DUTY
    # Steering: proportional to bearing (deg → duty)
    steer = max(-40.0, min(40.0, bearing * core_config.DOCK_STEER_GAIN))
    left = duty - steer
    right = duty + steer
    decision = apply_wheel_speeds(left, right, owner="docking_service", mode=RETURN_TO_DOCK)
    if not decision.get("applied"):
        _handle_gate_refusal(sess, decision)
    else:
        with _lock:
            if _session and _session["id"] == sess["id"]:
                _session["blocked_since"] = None


def _state_align(sess: dict) -> None:
    """Rotate until bearing within tolerance, then dock."""
    from services.motor_service import apply_wheel_speeds
    det = _latest_detection(sess["tag_id"], max_age_s=core_config.DOCK_TAG_LOST_GRACE_S)
    if det is None or det.get("bearing_deg") is None:
        # Can't read bearing (e.g. no intrinsics) — accept position as docked
        _end_session_docked()
        return

    bearing = det["bearing_deg"]
    if abs(bearing) <= core_config.DOCK_ALIGN_TOL_DEG:
        _end_session_docked()
        return

    direction = 1 if bearing > 0 else -1
    decision = apply_wheel_speeds(direction * core_config.DOCK_ALIGN_DUTY,
                                  -direction * core_config.DOCK_ALIGN_DUTY,
                                  owner="docking_service", mode=RETURN_TO_DOCK)
    if not decision.get("applied"):
        _handle_gate_refusal(sess, decision)


# ── Engine ───────────────────────────────────────────────────────────────────

def _engine_loop() -> None:
    handlers = {"SEEK": _state_seek, "APPROACH": _state_approach, "ALIGN": _state_align}
    logger.info("Docking engine started (tick=%.2fs)", ENGINE_TICK_S)
    while not _engine_stop.is_set():
        sess = status()
        if sess is None:
            _engine_stop.wait(ENGINE_TICK_S)
            continue

        st = get_robot_state()
        if st.is_estop_active() or st.get_mode() != RETURN_TO_DOCK:
            reason = "estop" if st.is_estop_active() else f"mode_changed_to_{st.get_mode()}"
            cancel_return(reason)
            continue

        handler = handlers.get(sess["state"])
        if handler:
            try:
                handler(sess)
            except Exception as exc:
                logger.error("Dock %s error in %s: %s", sess["id"], sess["state"], exc)
                cancel_return("engine_error")
        _engine_stop.wait(ENGINE_TICK_S)
    logger.info("Docking engine stopped")


def start_engine() -> None:
    global _engine_thread
    if _engine_thread is not None and _engine_thread.is_alive():
        return
    _engine_stop.clear()
    _engine_thread = threading.Thread(target=_engine_loop, name="sentra-docking", daemon=True)
    _engine_thread.start()


def stop_engine() -> None:
    _engine_stop.set()
