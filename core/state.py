"""
SENTRA — Central robot state (single source of truth).

Every part of the backend reads mode / e-stop / motor truth from here.
Future services (navigation, patrol, docking...) request modes; the state
machine arbitrates who is allowed to drive the motors.

Mode set (Phase 1):
    MANUAL          — human drives via /ws/control or REST
    PATROL          — autonomous wander loop (current behaviour, upgraded in Phase 7)
    NAVIGATION      — point-to-point drive (future phases)
    FOLLOW_PERSON   — future phase
    RETURN_TO_DOCK  — future phase
    EMERGENCY_STOP  — latched e-stop, motors cut
    STANDBY         — idle, motors stopped

Only one movement mode controls the motors at a time.
This module has NO hardware imports — safe to unit-test anywhere.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Mode definitions ──────────────────────────────────────────────────────────

MANUAL = "MANUAL"
PATROL = "PATROL"
NAVIGATION = "NAVIGATION"
FOLLOW_PERSON = "FOLLOW_PERSON"
RETURN_TO_DOCK = "RETURN_TO_DOCK"
EMERGENCY_STOP = "EMERGENCY_STOP"
STANDBY = "STANDBY"

MOVEMENT_MODES = {MANUAL, PATROL, NAVIGATION, FOLLOW_PERSON, RETURN_TO_DOCK}
ALL_MODES = MOVEMENT_MODES | {EMERGENCY_STOP, STANDBY}

# Which service/source may command each movement mode.
# e.g. the patrol loop may drive PATROL, the WS handler drives MANUAL.
MODE_OWNERS = {
    MANUAL: "control_ws",
    PATROL: "patrol_service",
    NAVIGATION: "navigation_service",
    FOLLOW_PERSON: "person_service",
    RETURN_TO_DOCK: "docking_service",
}


class RobotState:
    """Thread-safe central robot state. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode: str = STANDBY
        self._previous_mode: str = STANDBY
        self._estop_active: bool = False
        self._estop_reason: Optional[str] = None
        self._estop_at: Optional[float] = None
        self._mode_owner: Optional[str] = None       # source controlling motors now
        self._mode_changed_at: float = time.time()
        self._motors_disabled: bool = False          # latch (e-stop or manual disable)
        self._last_motion_at: float = 0.0

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_mode(self) -> str:
        with self._lock:
            return self._mode

    def get_previous_mode(self) -> str:
        with self._lock:
            return self._previous_mode

    def get_mode_owner(self) -> Optional[str]:
        with self._lock:
            return self._mode_owner

    def is_estop_active(self) -> bool:
        with self._lock:
            return self._estop_active

    def are_motors_disabled(self) -> bool:
        with self._lock:
            return self._motors_disabled

    def is_moving(self) -> bool:
        """True while a movement mode is active and motors are allowed to spin."""
        with self._lock:
            return self._mode in MOVEMENT_MODES and not self._estop_active and not self._motors_disabled

    def get_last_motion_at(self) -> float:
        with self._lock:
            return self._last_motion_at

    def snapshot(self) -> dict:
        """Consistent view for telemetry / debugging."""
        with self._lock:
            return {
                "mode": self._mode,
                "previous_mode": self._previous_mode,
                "mode_owner": self._mode_owner,
                "estop_active": self._estop_active,
                "estop_reason": self._estop_reason,
                "estop_at": self._estop_at,
                "motors_disabled": self._motors_disabled,
                "moving": self._mode in MOVEMENT_MODES and not self._estop_active and not self._motors_disabled,
                "mode_changed_at": self._mode_changed_at,
                "last_motion_at": self._last_motion_at,
            }

    # ── E-stop ────────────────────────────────────────────────────────────────

    def trigger_estop(self, reason: str = "manual") -> dict:
        """
        Latch EMERGENCY_STOP. Idempotent — first reason wins.
        Any component may trigger this; nothing may override it.
        """
        with self._lock:
            if self._estop_active:
                return {"estop_active": True, "status": "E-STOP ALREADY ACTIVE",
                        "reason": self._estop_reason}
            self._previous_mode = self._mode
            self._mode = EMERGENCY_STOP
            self._mode_owner = None
            self._estop_active = True
            self._estop_reason = reason
            self._estop_at = time.time()
            self._motors_disabled = True
            self._mode_changed_at = time.time()
        logger.critical("E-STOP latched (reason=%s, previous_mode=%s)", reason, self._previous_mode)
        return {"estop_active": True, "motors_disabled": True,
                "status": "E-STOP ACTIVATED", "reason": reason}

    def reset_estop(self) -> dict:
        """Clear the e-stop latch; robot returns to STANDBY (never auto-resumes motion)."""
        with self._lock:
            if not self._estop_active:
                return {"estop_active": False, "motors_disabled": False, "status": "NOT LATCHED"}
            self._estop_active = False
            self._estop_reason = None
            self._estop_at = None
            self._motors_disabled = False
            self._mode = STANDBY
            self._mode_owner = None
            self._mode_changed_at = time.time()
        logger.info("E-STOP reset → STANDBY")
        return {"estop_active": False, "motors_disabled": False, "status": "READY"}

    def disable_motors(self, reason: str = "manual") -> None:
        with self._lock:
            self._motors_disabled = True
        logger.info("Motors disabled (%s)", reason)

    def enable_motors(self) -> None:
        with self._lock:
            if self._estop_active:
                logger.warning("enable_motors ignored — e-stop still latched")
                return
            self._motors_disabled = False
        logger.info("Motors enabled")

    # ── Mode transitions ──────────────────────────────────────────────────────

    def request_mode(self, mode: str, requested_by: str) -> dict:
        """
        Request a mode change. Returns a result dict; callers must check 'accepted'.

        Rules:
          - Unknown mode                 → rejected
          - E-stop latched               → only reset (handled separately) clears it
          - Same mode                    → accepted (idempotent, owner may be set)
          - Movement mode takeover       → allowed only if current mode is STANDBY,
            same as requested, or the current owner is releasing implicitly.
            EMERGENCY_STOP cannot be requested directly (use trigger_estop).
        """
        with self._lock:
            if mode not in ALL_MODES:
                return {"accepted": False, "error": f"unknown mode: {mode}"}
            if mode == EMERGENCY_STOP:
                return {"accepted": False, "error": "use trigger_estop() for EMERGENCY_STOP"}

            if self._estop_active:
                return {"accepted": False, "error": "e-stop is latched; reset it first"}

            if mode == STANDBY:
                self._previous_mode = self._mode
                self._mode = STANDBY
                self._mode_owner = None
                self._mode_changed_at = time.time()
                logger.info("Mode → STANDBY (requested_by=%s)", requested_by)
                return {"accepted": True, "mode": STANDBY}

            if self._mode == mode:
                # Idempotent — but adopt the new owner so the right loop drives.
                self._mode_owner = self._resolve_owner(mode, requested_by)
                return {"accepted": True, "mode": mode}

            # Movement-mode transition: allowed from STANDBY or another movement mode.
            # The safety layer is responsible for stopping motors on the way out
            # (mode_exit_hook below); the state machine only arbitrates ownership.
            if self._mode in MOVEMENT_MODES or self._mode == STANDBY:
                self._previous_mode = self._mode
                self._mode = mode
                self._mode_owner = self._resolve_owner(mode, requested_by)
                self._mode_changed_at = time.time()
                logger.info("Mode %s → %s (requested_by=%s)",
                            self._previous_mode, mode, requested_by)
                return {"accepted": True, "mode": mode, "previous": self._previous_mode}

            return {"accepted": False, "error": f"cannot switch from {self._mode} to {mode}"}

    @staticmethod
    def _resolve_owner(mode: str, requested_by: str) -> str:
        """Who will actually drive the motors in this mode?
        For MANUAL, whoever requested it drives (a WS connection id) — unless
        the request came from the control plane (REST/system), in which case
        the canonical WS driver is expected to take over.
        For autonomous modes, the canonical service loop is always the driver."""
        if mode == MANUAL and requested_by not in ("rest_api", "system"):
            return requested_by
        return MODE_OWNERS.get(mode, requested_by)

    def note_motion(self) -> None:
        """Called by the safety layer whenever a motor command actually passes."""
        with self._lock:
            self._last_motion_at = time.time()


# ── Process-wide singleton ────────────────────────────────────────────────────

_robot_state = RobotState()


def get_robot_state() -> RobotState:
    return _robot_state
