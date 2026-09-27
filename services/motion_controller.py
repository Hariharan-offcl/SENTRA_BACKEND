"""
SENTRA — Motion controller (Phase 2).

The single MANUAL-mode authority. Both REST one-shot commands
(/api/v1/control/*) and streaming commands (/ws/control) feed this
controller; it owns a 20 Hz ramp loop that pushes smoothed duty through the
centralized safety gate (core.safety).

Features:
    - Wheel-speed targets (left/right signed duty -100..100)
    - Directional commands: FORWARD BACKWARD LEFT RIGHT STOP BRAKE (+ scale)
    - Legacy differential (linear_velocity/angular_velocity) preserved
    - Acceleration / deceleration limiting (core.config rates)
    - Command freshness: a streaming source that goes silent is ramped to stop
    - One-shot REST moves carry a duration and auto-stop afterwards
    - Active brake via L298N shorted-terminals (motor_service.apply_active_brake)

Hardware-independent: the safety layer is a singleton whose motor callable is
injected; in dev mode (no lgpio) everything runs as a no-op simulation.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from core import config as core_config
from core.safety_config import get_safety_config
from core.state import (
    get_robot_state,
    MANUAL,
    PATROL,
    NAVIGATION,
    RETURN_TO_DOCK,
    STANDBY,
    MOVEMENT_MODES,
)
from core.safety import get_safety_layer

logger = logging.getLogger(__name__)

DIRECTIONAL = {"FORWARD", "BACKWARD", "LEFT", "RIGHT"}
_OWNER = "motion_controller"


class MotionController:
    """Ramp loop + target bookkeeping for MANUAL movement."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._target_left: float = 0.0
        self._target_right: float = 0.0
        self._current_left: float = 0.0
        self._current_right: float = 0.0
        self._target_active: bool = False        # True while a target is held
        self._source: str = ""
        self._last_source_cmd: float = 0.0
        self._one_shot_until: Optional[float] = None
        self._brake_requested: bool = False
        self._braking: bool = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._ramp_loop,
                                        name="sentra-motion", daemon=True)
        self._thread.start()
        logger.info("Motion controller started (tick=%.2fs, accel=%.0f%%/s, decel=%.0f%%/s)",
                    core_config.MOTION_TICK_S, core_config.ACCEL_PCT_PER_S,
                    core_config.DECEL_PCT_PER_S)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._target_active = False

    # ── Mode authority ────────────────────────────────────────────────────────

    def _ensure_manual(self, source: str) -> bool:
        """Become the MANUAL authority. Returns False if blocked (e-stop)."""
        st = get_robot_state()
        if st.is_estop_active():
            return False
        if st.get_mode() == MANUAL:
            # Adopt ownership if another MANUAL owner is registered.
            if st.get_mode_owner() not in (None, _OWNER):
                st.request_mode(MANUAL, requested_by=_OWNER)
            return True
        if st.get_mode() == STANDBY or st.get_mode() in MOVEMENT_MODES:
            previous = st.get_mode()
            result = st.request_mode(MANUAL, requested_by=_OWNER)
            if result.get("accepted") and previous in (PATROL, RETURN_TO_DOCK, NAVIGATION):
                # User took over — end any autonomy session cleanly.
                try:
                    from services import patrol_service
                    patrol_service.stop_patrol()
                except Exception:
                    from services.motor_service import stop_patrol_loop
                    stop_patrol_loop()
                try:
                    from services import docking_service
                    docking_service.cancel_return("manual_takeover")
                except Exception:
                    pass
                try:
                    from services import navigation_service
                    navigation_service.cancel("manual_takeover")
                except Exception:
                    pass
            return bool(result.get("accepted"))
        return False

    # ── Target setters ────────────────────────────────────────────────────────

    def set_wheel_target(self, left: float, right: float, source: str,
                         stream: bool = True, duration: Optional[float] = None) -> dict:
        """Set signed duty targets (-100..100). stream=True → kept alive by
        repeated commands; stream=False → expires after `duration` seconds."""
        left = _clamp100(float(left))
        right = _clamp100(float(right))
        if not (math.isfinite(left) and math.isfinite(right)):
            return {"applied": False, "error": "invalid_speed"}

        if not self._ensure_manual(source):
            return {"applied": False, "error": "estop_or_mode_blocked"}

        with self._lock:
            self._target_left = left
            self._target_right = right
            self._target_active = True
            self._source = source
            self._last_source_cmd = time.time()
            self._one_shot_until = (
                time.time() + min(duration, core_config.MAX_MOVE_DURATION_S)
                if (not stream and duration is not None) else None
            )
            self._brake_requested = False
        return {"applied": True, "left": left, "right": right}

    def set_differential(self, linear: float, angular: float, source: str,
                         speed_multiplier: float) -> dict:
        """Legacy /ws/control form: linear/angular in -1..1."""
        linear = max(-1.0, min(1.0, float(linear)))
        angular = max(-1.0, min(1.0, float(angular)))
        left = (linear - angular) * 100.0 * speed_multiplier
        right = (linear + angular) * 100.0 * speed_multiplier
        return self.set_wheel_target(left, right, source, stream=True)

    def set_directional(self, direction: str, scale: float, source: str,
                        duration: Optional[float] = None, stream: bool = True) -> dict:
        """FORWARD/BACKWARD/LEFT/RIGHT with scale 0..1."""
        direction = str(direction).upper()
        scale = max(0.0, min(1.0, float(scale)))
        mapping = {
            "FORWARD": (scale, scale),
            "BACKWARD": (-scale, -scale),
            "LEFT": (-scale, scale),
            "RIGHT": (scale, -scale),
        }
        if direction not in mapping:
            return {"applied": False, "error": f"unknown_direction:{direction}"}
        left, right = mapping[direction]
        return self.set_wheel_target(left * 100.0, right * 100.0, source,
                                     stream=stream, duration=duration)

    def stop(self, source: str) -> dict:
        """Ramp to zero (fast decel). Always allowed."""
        with self._lock:
            self._target_left = 0.0
            self._target_right = 0.0
            self._target_active = False
            self._one_shot_until = None
            self._brake_requested = False
        return {"applied": True, "action": "stop"}

    def brake(self, source: str) -> dict:
        """Fast decel to zero, then active (shorted-terminal) brake pulse."""
        with self._lock:
            self._target_left = 0.0
            self._target_right = 0.0
            self._target_active = False
            self._one_shot_until = None
            self._brake_requested = True
        return {"applied": True, "action": "brake"}

    # ── Introspection (for WS state push / REST state) ────────────────────────

    def get_state(self) -> dict:
        st = get_robot_state()
        with self._lock:
            return {
                "current_left": round(self._current_left, 1),
                "current_right": round(self._current_right, 1),
                "target_left": round(self._target_left, 1),
                "target_right": round(self._target_right, 1),
                "braking": self._braking or self._brake_requested,
                "active": self._target_active,
                "mode": st.get_mode(),
                "estop_active": st.is_estop_active(),
                "moving": st.is_moving(),
                "speed_multiplier": _speed_multiplier(),
            }

    # ── Unified message handling (used by /ws/control) ────────────────────────

    def handle_message(self, payload: dict) -> dict:
        """Accept one control message (any supported form) → ACK dict.
        Legacy field names and the legacy ACK shape stay compatible."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        mtype = str(payload.get("type", "") or "").lower()
        direction = str(payload.get("direction", "") or "").upper()

        # 1) Wheel-speed form  {"type":"wheel","left":50,"right":50}
        #    or {"left_speed":..,"right_speed":..}
        if mtype == "wheel" or ("left_speed" in payload and "right_speed" in payload):
            left = float(payload.get("left", payload.get("left_speed")))
            right = float(payload.get("right", payload.get("right_speed")))
            result = self.set_wheel_target(left, right, "control_ws")
            return {"ack": True, "type": "wheel", **result}

        # 2) Legacy differential form (linear/angular present) — must be checked
        #    before bare directional words so legacy payloads keep their meaning.
        if "linear_velocity" in payload or "angular_velocity" in payload:
            if direction == "STOP":
                result = self.stop("control_ws")
                return {"ack": True, "direction": "STOP", **result}
            linear = float(payload.get("linear_velocity", 0.0))
            angular = float(payload.get("angular_velocity", 0.0))
            result = self.set_differential(linear, angular, "control_ws",
                                           _speed_multiplier())
            return {"ack": True, "direction": direction or "DIFFERENTIAL", **result}

        # 3) Explicit stop / brake
        if mtype in ("stop", "brake") or direction in ("STOP", "BRAKE"):
            if mtype == "brake" or direction == "BRAKE":
                result = self.brake("control_ws")
                return {"ack": True, "direction": "BRAKE", **result}
            result = self.stop("control_ws")
            return {"ack": True, "direction": "STOP", **result}

        # 4) Directional word  {"type":"direction","direction":"FORWARD","scale":0.5}
        #    or {"direction":"FORWARD"} / {"direction":"LEFT","scale":0.8}
        if mtype == "direction" or direction in DIRECTIONAL:
            d = direction if direction in DIRECTIONAL else str(payload.get("direction", "")).upper()
            if d not in DIRECTIONAL:
                raise ValueError(f"unknown direction: {d}")
            scale = float(payload.get("scale", 0.5))
            result = self.set_directional(d, scale, "control_ws")
            return {"ack": True, "direction": d, **result}

        raise ValueError("unrecognized control message")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp100(v: float) -> float:
    return max(-100.0, min(100.0, v))


def _speed_multiplier() -> float:
    from services.motor_service import _state
    return _state["speed_multiplier"]


def _step_toward(current: float, target: float, dt: float) -> float:
    """Move current toward target at accel/decel rate limits (runtime-tunable)."""
    if math.isclose(current, target, abs_tol=0.01):
        return target
    cfg = get_safety_config()
    delta_mag = abs(target) < abs(current) or (current * target < 0)
    rate = cfg.get("decel_pct_per_s") if delta_mag else cfg.get("accel_pct_per_s")
    max_step = rate * dt
    diff = target - current
    if abs(diff) <= max_step:
        return target
    return current + math.copysign(max_step, diff)


# ── Ramp loop ────────────────────────────────────────────────────────────────

def _ramp_loop(self: MotionController) -> None:
    safety = get_safety_layer()
    last = time.time()
    while not self._stop_event.is_set():
        now = time.time()
        dt = min(now - last, 0.2)
        last = now

        # 1) Expiry rules → zero target when a source goes silent or one-shot ends
        with self._lock:
            if self._target_active and self._source and self._one_shot_until is None:
                # streaming source must keep feeding commands
                if now - self._last_source_cmd > core_config.MANUAL_CMD_TIMEOUT_S:
                    logger.info("Motion target expired (source silent: %s)", self._source)
                    self._target_left = 0.0
                    self._target_right = 0.0
                    self._target_active = False
            if self._one_shot_until is not None and now > self._one_shot_until:
                logger.info("Motion one-shot duration elapsed — auto stop")
                self._target_left = 0.0
                self._target_right = 0.0
                self._one_shot_until = None
                self._target_active = False
            target_l, target_r = self._target_left, self._target_right

        # 2) Ramp current toward target
        new_l = _step_toward(self._current_left, target_l, dt)
        new_r = _step_toward(self._current_right, target_r, dt)

        # 3) Apply through the safety gate whenever there is something to drive
        #    (also feeds the watchdog while cruising).
        moving_or_stopping = (new_l, new_r) != (0.0, 0.0) or (self._current_left, self._current_right) != (0.0, 0.0)
        if moving_or_stopping:
            decision = safety.check_and_apply(_OWNER, MANUAL, new_l, new_r)
            if decision.get("applied"):
                self._current_left, self._current_right = decision["left"], decision["right"]
            else:
                if decision.get("reason") in ("estop", "disabled", "timeout", "mode_mismatch"):
                    # hardware already zeroed by the gate — stay consistent
                    self._current_left = 0.0
                    self._current_right = 0.0
        else:
            self._current_left = 0.0
            self._current_right = 0.0
            safety.note_command(_OWNER)  # keep watchdog fed while idle in MANUAL

        # 4) Active brake pulse once fully stopped.
        #    Runs in a helper thread so the 0.5 s pulse never blocks ramping
        #    or watchdog feeding (a new command mid-pulse must still work).
        with self._lock:
            if self._brake_requested and self._current_left == 0.0 and self._current_right == 0.0:
                self._brake_requested = False
                self._braking = True
                do_brake = True
            else:
                do_brake = False
        if do_brake:
            def _pulse():
                try:
                    from services.motor_service import apply_active_brake
                    apply_active_brake()
                finally:
                    with self._lock:
                        self._braking = False
            threading.Thread(target=_pulse, name="sentra-brake-pulse", daemon=True).start()

        time.sleep(core_config.MOTION_TICK_S)


# Attach the loop (kept outside the class body for readability)
MotionController._ramp_loop = _ramp_loop  # type: ignore[attr-defined]


# ── Process-wide singleton ────────────────────────────────────────────────────

_motion = MotionController()


def get_motion_controller() -> MotionController:
    return _motion
