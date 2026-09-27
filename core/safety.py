"""
SENTRA — Centralized safety layer.

THE rule: no code drives motors directly. Everything goes through
`check_and_apply()` here, which enforces, in order:

    1. E-stop latched            → STOP
    2. Motors disabled latch     → STOP
    3. Mode/owner match          → REJECT (autonomy cannot grab motors from MANUAL)
    4. Command timeout watchdog  → STOP (no fresh command → no motion)
    5. Obstacle checks           → STOP (front/rear/cliff, from unified sensor state)
    6. Speed clamp               → CLAMP to ceiling
    7. Direction validation      → REJECT non-finite duty values

Only after all checks pass is the low-level motor driver invoked.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from core import config as core_config
from core.safety_config import get_safety_config
from core.state import get_robot_state

logger = logging.getLogger(__name__)


class SafetyLayer:
    """Thread-safe motor-command gate.

    The motor driver is injected as a callable so this module stays
    hardware-independent and unit-testable on any machine.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_cmd_at: dict[str, float] = {}   # owner → last fresh command time
        self._owner_modes: dict[str, str] = {}     # owner → mode it commands in
        self._watchdog_started = False
        # sensor_provider() → dict with keys like front_distance_m / rear_distance_m
        # and cliff flags; injected by main.py so safety never imports hardware.
        self._sensor_provider: Optional[Callable[[], dict]] = None
        # The one and only motor application fn: (left_pct, right_pct) → None
        self._motor_apply: Optional[Callable[[float, float], None]] = None
        # Optional event reporter (wired to services.safety_events in main.py);
        # injected so core/ stays free of service-layer imports.
        # signature: (event_type: str, detail: dict, severity: str) -> None
        self._event_reporter: Optional[Callable[[str, dict, str], None]] = None
        self._last_decision: dict = {"applied": False, "reason": "init"}

    # ── Wiring (called once from main.py / tests) ────────────────────────────

    def wire(self, motor_apply: Callable[[float, float], None],
             sensor_provider: Optional[Callable[[], dict]] = None) -> None:
        with self._lock:
            self._motor_apply = motor_apply
            self._sensor_provider = sensor_provider

    def set_sensor_provider(self, sensor_provider: Callable[[], dict]) -> None:
        with self._lock:
            self._sensor_provider = sensor_provider

    def wire_event_reporter(self, reporter: Callable[[str, dict, str], None]) -> None:
        with self._lock:
            self._event_reporter = reporter

    def _report(self, event_type: str, detail: dict, severity: str = "WARNING") -> None:
        with self._lock:
            reporter = self._event_reporter
        if reporter is None:
            return
        try:
            reporter(event_type, detail, severity)
        except Exception as exc:
            logger.error("event reporter failed: %s", exc)

    # ── Sensor helpers ────────────────────────────────────────────────────────

    def _read_sensors(self) -> dict:
        with self._lock:
            provider = self._sensor_provider
        if provider is None:
            return {}
        try:
            return provider() or {}
        except Exception as exc:  # sensor failure must never crash the gate
            logger.error("sensor provider failed: %s", exc)
            return {}

    @staticmethod
    def _front_distance(sensors: dict) -> Optional[float]:
        v = sensors.get("front_distance_m")
        return float(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _rear_distance(sensors: dict) -> Optional[float]:
        v = sensors.get("rear_distance_m")
        return float(v) if isinstance(v, (int, float)) else None

    def cliff_triggered(self, sensors: dict) -> bool:
        if not get_safety_config().get_bool("cliff_stop"):
            return False
        left = sensors.get("left_cliff", False)
        right = sensors.get("right_cliff", False)
        return bool(left) or bool(right)

    def status(self) -> dict:
        """Live safety status for the /safety/status endpoint."""
        st = get_robot_state()
        cfg = get_safety_config().snapshot()
        return {
            "mode": st.get_mode(),
            "estop_active": st.is_estop_active(),
            "motors_disabled": st.are_motors_disabled(),
            "moving": st.is_moving(),
            "watchdog_active": self._watchdog_started,
            "last_decision": dict(self._last_decision),
            "sensors": self._read_sensors(),
            "thresholds": cfg,
        }

    def read_sensors(self) -> dict:
        """Public sensor read for services (patrol path checks).
        Returns a copy — callers cannot mutate gate state."""
        return self._read_sensors()

    # ── Watchdog bookkeeping ─────────────────────────────────────────────────

    def note_command(self, owner: str) -> None:
        """Record a fresh command arrival for the timeout watchdog."""
        with self._lock:
            self._last_cmd_at[owner] = time.time()

    def last_command_age(self, owner: str) -> Optional[float]:
        with self._lock:
            ts = self._last_cmd_at.get(owner)
        return (time.time() - ts) if ts is not None else None

    def _timeout_for(self, mode: str) -> float:
        cfg = get_safety_config()
        return (cfg.get("manual_cmd_timeout_s")
                if mode == "MANUAL" else cfg.get("autonomous_cmd_timeout_s"))

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def start_watchdog(self) -> None:
        """Start the background watchdog that stops motors when commands stall.
        Covers the case where a control loop dies without sending STOP."""
        with self._lock:
            if self._watchdog_started:
                return
            self._watchdog_started = True
        thread = threading.Thread(target=self._watchdog_loop,
                                  name="sentra-safety-watchdog", daemon=True)
        thread.start()
        logger.info("Safety watchdog started (tick=%.2fs)", WATCHDOG_TICK_S)

    def _watchdog_loop(self) -> None:
        while True:
            now = time.time()
            with self._lock:
                expired = [
                    (owner, mode) for owner, mode in list(self._owner_modes.items())
                    if now - self._last_cmd_at.get(owner, now) > self._timeout_for(mode)
                ]
                for owner, _mode in expired:
                    # Clear entries so the next fresh command is not re-blocked
                    # by a stale timestamp.
                    self._last_cmd_at.pop(owner, None)
                    self._owner_modes.pop(owner, None)
            if expired:
                logger.warning("SAFETY WATCHDOG: command timeout for %s — stopping motors",
                               [o for o, _ in expired])
                self._zero_motors()
                self._report("TIMEOUT", {"owners": [o for o, _ in expired],
                                         "source": "watchdog"})
            time.sleep(WATCHDOG_TICK_S)

    # ── THE gate ─────────────────────────────────────────────────────────────

    def check_and_apply(
        self,
        owner: str,
        mode: str,
        left_pct: float,
        right_pct: float,
    ) -> dict:
        """
        Request motor output (percent duty, -100..100) for `owner` in `mode`.
        Returns a decision dict:
            {"applied": True,  "left": .., "right": ..}
            {"applied": False, "reason": "estop|disabled|timeout|obstacle|
                                          mode_mismatch|invalid_direction|not_wired"}
        """
        st = get_robot_state()

        cfg = get_safety_config()

        # 1) E-stop
        if st.is_estop_active():
            return self._stop_only("estop")

        # 2) Disabled latch
        if st.are_motors_disabled():
            return self._stop_only("disabled")

        # 3) Mode arbitration — only the mode owner may drive.
        #    A mismatch means another mode took over: stop cleanly instead of
        #    leaving stale PWM on the wires (clean takeover).
        if st.get_mode() != mode:
            self._zero_motors()
            return self._decide({"applied": False, "reason": "mode_mismatch",
                                 "current_mode": st.get_mode()})
        owner_now = st.get_mode_owner()
        if owner_now is not None and owner_now != owner:
            self._zero_motors()
            return self._decide({"applied": False, "reason": "mode_mismatch",
                                 "current_mode": st.get_mode(), "owner": owner_now})

        # 7a) Direction validation — reject NaN/inf early
        if not (math.isfinite(left_pct) and math.isfinite(right_pct)):
            return {"applied": False, "reason": "invalid_direction"}

        # 4) Command timeout watchdog — age is from the PREVIOUS command;
        #    this command's freshness is recorded only after it passes.
        timeout = self._timeout_for(mode)
        age = self.last_command_age(owner)
        if age is not None and age > timeout:
            with self._lock:
                self._last_cmd_at.pop(owner, None)
                self._owner_modes.pop(owner, None)
            self._zero_motors()
            self._report("TIMEOUT", {"owner": owner, "age_s": round(age, 3),
                                     "timeout_s": timeout})
            return self._decide({"applied": False, "reason": "timeout",
                                 "age_s": round(age, 3), "timeout_s": timeout})

        # 5) Obstacle checks — STOP FIRST, never steer around.
        sensors = self._read_sensors()
        moving_forward = (left_pct > 0 and right_pct > 0)
        moving_backward = (left_pct < 0 and right_pct < 0)

        if self.cliff_triggered(sensors):
            self._zero_motors()
            logger.warning("SAFETY STOP: cliff sensor triggered (owner=%s)", owner)
            self._report("CLIFF", {"owner": owner}, severity="DANGER")
            return self._decide({"applied": False, "reason": "obstacle",
                                 "detail": {"sensor": "cliff"}})

        if moving_forward:
            front = self._front_distance(sensors)
            front_stop = cfg.get("front_obstacle_stop_m")
            if front is not None and front < front_stop:
                self._zero_motors()
                logger.warning("SAFETY STOP: front obstacle %.2fm < %.2fm (owner=%s)",
                               front, front_stop, owner)
                self._report("OBSTACLE", {"sensor": "front", "distance_m": front,
                                          "threshold_m": front_stop})
                return self._decide({"applied": False, "reason": "obstacle",
                                     "detail": {"sensor": "front", "distance_m": front,
                                                "threshold_m": front_stop}})

        if moving_backward:
            rear = self._rear_distance(sensors)
            rear_stop = cfg.get("rear_obstacle_stop_m")
            if rear is not None and rear < rear_stop:
                self._zero_motors()
                logger.warning("SAFETY STOP: rear obstacle %.2fm < %.2fm (owner=%s)",
                               rear, rear_stop, owner)
                self._report("OBSTACLE", {"sensor": "rear", "distance_m": rear,
                                          "threshold_m": rear_stop})
                return self._decide({"applied": False, "reason": "obstacle",
                                     "detail": {"sensor": "rear", "distance_m": rear,
                                                "threshold_m": rear_stop}})

        # 6) Speed clamp (runtime-configurable ceiling)
        max_abs = 100.0 * cfg.get("max_speed_multiplier")
        left_pct = max(-max_abs, min(max_abs, left_pct))
        right_pct = max(-max_abs, min(max_abs, right_pct))

        # All checks passed → apply
        apply_fn = self._motor_apply
        if apply_fn is None:
            return self._decide({"applied": False, "reason": "not_wired"})
        apply_fn(left_pct, right_pct)
        with self._lock:
            self._last_cmd_at[owner] = time.time()
            self._owner_modes[owner] = mode
        st.note_motion()
        return self._decide({"applied": True, "left": left_pct, "right": right_pct})

    def force_stop(self, reason: str = "forced") -> dict:
        """Unconditionally zero motors (STOP is always allowed through)."""
        with self._lock:
            self._owner_modes.clear()
            self._last_cmd_at.clear()
        self._zero_motors()
        logger.info("Safety force_stop (%s)", reason)
        return {"applied": False, "reason": "stopped", "forced": reason}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _decide(self, decision: dict) -> dict:
        with self._lock:
            self._last_decision = dict(decision)
        return decision

    def _zero_motors(self) -> None:
        apply_fn = self._motor_apply
        if apply_fn is not None:
            try:
                apply_fn(0.0, 0.0)
            except Exception as exc:
                logger.error("zero_motors failed: %s", exc)

    def _stop_only(self, reason: str) -> dict:
        self._zero_motors()
        if reason == "estop":
            self._report("ESTOP", {"phase": "command_gate"}, severity="DANGER")
        elif reason == "mode_mismatch":
            self._report("MODE_MISMATCH", {"requested_by": "gate"})
        return self._decide({"applied": False, "reason": reason})


# ── Process-wide singleton ────────────────────────────────────────────────────

WATCHDOG_TICK_S = 0.2

_safety = SafetyLayer()


def get_safety_layer() -> SafetyLayer:
    return _safety
