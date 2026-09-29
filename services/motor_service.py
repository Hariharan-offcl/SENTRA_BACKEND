"""
SENTRA — Motor / PWM abstraction layer (Phase 1 refactor).

Integrates with lgpio on Raspberry Pi 5. All commands now pass through the
centralized safety layer (core/safety.py) and the central robot state
(core/state.py). The public functions used by routers and WS handlers are
unchanged:

    apply_locomotion(linear_velocity, angular_velocity, direction)
    trigger_estop() / reset_estop() / set_mode(mode) / set_speed(...)
    get_state()

New in Phase 1:
    apply_wheel_speeds(left_pct, right_pct, owner, mode)  — canonical safety-gated entry
    start_patrol_loop() / stop_patrol_loop()              — explicit lifecycle (no import side effects)
"""

import atexit
import logging
import threading
import time

from config import settings
from core import config as core_config
from core.safety_config import get_safety_config
from core.state import (
    get_robot_state,
    MANUAL,
    PATROL,
    STANDBY,
)
from core.safety import get_safety_layer

logger = logging.getLogger(__name__)

# Try to load lgpio (will fail on Windows, so we wrap it)
try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False
    logger.warning("lgpio not found. Motor commands will be simulated (dev mode).")

# ── Hardware Pin Configuration ────────────────────────────────────────────────
# Left Motor (A) - Flipped to drive Forward (commit: "Motor direction problem solved")
ENA, IN1, IN2 = 12, 27, 17
# Right Motor (B) - Flipped to drive Forward
ENB, IN3, IN4 = 13, 22, 23

# Shared in-memory state (compat view; authoritative truth is core.state)
_state = {
    "estop_active": False,
    "motors_disabled": False,
    "mode": STANDBY,
    "speed_multiplier": core_config.DEFAULT_SPEED_MULTIPLIER,
    "target_mps": 0.0,
}

_h = None                      # Phase 2: alias of the SHARED gpio_manager handle
_gpio_lock = threading.Lock()  # lgpio chip handle is not thread-safe


def _init_hardware():
    """Phase 2: claim the six L298N pins through the shared gpio_manager
    handle (the same one the HAL and every sensor service use) instead of
    opening a private gpiochip4. Simulation mode still refuses."""
    global _h
    from core.simulation import activate_once
    activate_once()  # Phase 18: simulation mode refuses GPIO init
    if not _LGPIO_AVAILABLE or core_config.SIMULATION:
        return
    from hardware.gpio_manager import gpio_manager
    h = gpio_manager.chip
    if h is None:
        logger.error("Motor service: no shared GPIO handle available")
        return
    # Owner "motor" is shared with the HAL PhysicalMotor on purpose: claims
    # are idempotent per owner, so HAL-first or service-first both succeed
    # and there is exactly ONE claim of the L298N pins on ONE handle.
    ok = all(gpio_manager.claim_output(pin, owner="motor", initial=0)
             for pin in (ENA, IN1, IN2, ENB, IN3, IN4))
    if ok:
        _h = h
        logger.info("SENTRA motor pins claimed on the shared GPIO handle")
    else:
        logger.error("Motor service: pin claim failed — running simulated")
        _h = None


def _cleanup_hardware():
    """Zero the motors; the shared handle itself is closed once by the HAL."""
    if _h is not None:
        try:
            _set_motor(ENA, IN1, IN2, 0)
            _set_motor(ENB, IN3, IN4, 0)
        except Exception as exc:
            logger.warning("Motor cleanup write failed: %s", exc)


# Initialize on startup (idempotent, claims via the SHARED gpio_manager
# handle), clean up on shutdown (no chip close here — gpio_manager owns it).
_init_hardware()
atexit.register(_cleanup_hardware)

# ── Stall Detection Guard (Phase 6) ──────────────────────────────────────────────

_stall_start_time: Optional[float] = None

def _check_for_stall():
    """
    Monitors the current draw via HAL. If current exceeds threshold
    for a sustained period, triggers an ESTOP.
    """
    global _stall_start_time
    from hardware.manager import hardware_manager

    # Only monitor if motors are actually commanded to move
    st = get_robot_state()
    if not st.is_moving():
        _stall_start_time = None
        return

    try:
        curr_data = hardware_manager.current_sensor.read()
        current_ma = abs(curr_data.get("current", 0.0))

        threshold = core_config.STALL_CURRENT_THRESHOLD_MA
        dwell = core_config.STALL_DWELL_TIME_S

        if current_ma > threshold:
            if _stall_start_time is None:
                _stall_start_time = time.time()
            elif (time.time() - _stall_start_time) > dwell:
                logger.warning("MOTOR STALL DETECTED: %.2f mA > %.2f mA", current_ma, threshold)
                trigger_estop() # This resets _stall_start_time effectively by stopping movement
        else:
            _stall_start_time = None

    except Exception as e:
        logger.error("Stall guard check error: %s", e)


def get_state() -> dict:
    """Compatibility view over the central state + local speed settings."""
    snap = get_robot_state().snapshot()
    return {
        "estop_active": snap["estop_active"],
        "motors_disabled": snap["motors_disabled"],
        "mode": snap["mode"],
        "speed_multiplier": _state["speed_multiplier"],
        "target_mps": _state["target_mps"],
    }


def _set_motor(en, a, b, speed_pct):
    """Low-level motor control (speed_pct: -100 to 100). Called by safety layer only.
    Phase 2: kept as a defensive direct path; in the normal build the drive
    traffic flows through _apply_wheel_duty → HAL → the same shared handle."""
    if _h is None:
        return  # Dev mode

    with _gpio_lock:
        if speed_pct > 0:
            lgpio.gpio_write(_h, a, 1)
            lgpio.gpio_write(_h, b, 0)
        elif speed_pct < 0:
            lgpio.gpio_write(_h, a, 0)
            lgpio.gpio_write(_h, b, 1)
        else:
            lgpio.gpio_write(_h, a, 0)
            lgpio.gpio_write(_h, b, 0)

        # PWM frequency 1000Hz, duty cycle 0-100
        lgpio.tx_pwm(_h, en, 1000, min(abs(speed_pct), 100))


def apply_active_brake() -> None:
    """Active brake: shorted motor terminals on the L298N, held for
    BRAKE_HOLD_S, then released to neutral.

    Phase 2 consolidation: routed through the HAL motor's brake() so the
    short-brake sequence runs on the SAME shared handle and pins as the
    drive path — never again through a second, privately-opened chip.
    Simulated drivers record the brake and return instantly (dev mode and
    tests must not stall); the real hold sleep only happens on hardware."""
    from hardware.manager import hardware_manager
    motor = hardware_manager.motor
    if motor is not None and getattr(motor, "initialized", False):
        motor.brake(core_config.BRAKE_HOLD_S)
    else:
        # No initialised HAL (dev box): behave like the old dev path — no-op
        # without a hardware hold sleep.
        _set_motor(ENA, IN1, IN2, 0)
        _set_motor(ENB, IN3, IN4, 0)


from hardware.manager import hardware_manager

def _apply_wheel_duty(left_pct: float, right_pct: float) -> None:
    """Raw differential application — the single function the safety layer drives."""
    # BRIDGE: Redirect to HAL
    hardware_manager.motor.set_speed(left_pct / 100.0, right_pct / 100.0)


# ── Canonical safety-gated entry point ───────────────────────────────────────

def apply_wheel_speeds(left_pct: float, right_pct: float,
                       owner: str, mode: str) -> dict:
    """
    THE way to move the rover. left/right are percent duty (-100..100).
    Goes through the safety layer; returns its decision dict.
    """
    decision = get_safety_layer().check_and_apply(owner, mode, left_pct, right_pct)
    if not decision.get("applied") and decision.get("reason") not in (
            "timeout", "estop", "disabled", "mode_mismatch"):
        logger.info("Motor CMD rejected  owner=%s  reason=%s", owner, decision.get("reason"))
    return decision


def stop_all(owner: str = "system") -> dict:
    """STOP always passes through the safety layer."""
    return get_safety_layer().force_stop(f"stop_all by {owner}")


# ── Legacy-compatible locomotion (used by /ws/control) ───────────────────────

def apply_locomotion(linear_velocity: float, angular_velocity: float, direction: str) -> None:
    st = get_robot_state()

    # STOP must never be blocked.
    if str(direction).upper() == "STOP":
        stop_all("control_ws")
        return

    # Auto-adopt MANUAL when the app starts driving from STANDBY (compat with
    # existing Flutter behaviour of not calling /robot/mode first).
    if st.get_mode() == STANDBY:
        result = st.request_mode(MANUAL, requested_by="control_ws")
        if not result.get("accepted"):
            logger.warning("Locomotion rejected — could not adopt MANUAL: %s", result)
            return

    logger.info("Motor CMD  dir=%s  linear=%.3f  angular=%.3f",
                direction, linear_velocity, angular_velocity)

    # Calculate differential drive
    left = linear_velocity - angular_velocity
    right = linear_velocity + angular_velocity

    # Clamp to -1.0 to 1.0
    left = max(-1.0, min(1.0, left))
    right = max(-1.0, min(1.0, right))

    # Convert to percentages for PWM (-100 to 100), scaled by speed multiplier
    mult = _state["speed_multiplier"]
    left_pct = left * 100 * mult
    right_pct = right * 100 * mult

    apply_wheel_speeds(left_pct, right_pct, owner="control_ws", mode=MANUAL)


# ── E-stop / mode / speed (REST contract unchanged) ──────────────────────────

def trigger_estop() -> dict:
    result = get_robot_state().trigger_estop(reason="rest_or_ws")
    # Immediate hardware cut via HAL
    hardware_manager.motor.set_speed(0, 0)
    return {
        "estop_active": result["estop_active"],
        "motors_disabled": result.get("motors_disabled", True),
        "status": result["status"],
    }


def reset_estop() -> dict:
    result = get_robot_state().reset_estop()
    return {
        "estop_active": result["estop_active"],
        "motors_disabled": result["motors_disabled"],
        "status": result["status"],
    }


def set_mode(mode: str) -> dict:
    """Switch movement mode. PATROL delegates to the Phase 7 patrol service
    (route session when routes exist, legacy wander otherwise)."""
    st = get_robot_state()

    if mode == PATROL:
        try:
            from services import patrol_service
            if patrol_service.get_default_route() is not None:
                result = patrol_service.start_patrol()
                if not result.get("ok"):
                    logger.warning("Patrol via routes failed: %s", result.get("error"))
                return {"active_mode": st.get_mode(), "status": "READY"}
        except Exception as exc:
            logger.error("Patrol service unavailable, falling back to wander: %s", exc)
        # Legacy wander (no routes / service error)
        result = st.request_mode(PATROL, requested_by="rest_api")
        if result.get("accepted"):
            start_patrol_loop()
        return {"active_mode": st.get_mode(), "status": "READY"}

    result = st.request_mode(mode, requested_by="rest_api")
    if not result.get("accepted"):
        logger.warning("Mode change rejected: %s", result)
        # Surface current state; REST contract has no error field, keep READY shape
    if mode != PATROL:
        # End any active autonomy (patrol / docking) on takeover
        try:
            from services import patrol_service
            patrol_service.stop_patrol()
        except Exception:
            stop_patrol_loop()
        try:
            from services import docking_service
            docking_service.cancel_return("mode_change")
        except Exception:
            pass
        try:
            from services import navigation_service
            navigation_service.cancel("mode_change")
        except Exception:
            pass
        stop_all("mode_change")
    return {"active_mode": st.get_mode(), "status": "READY"}


def set_speed(speed_multiplier: float, target_mps: float) -> dict:
    # Clamp to the runtime safety ceiling
    ceiling = get_safety_config().get("max_speed_multiplier")
    clamped = max(0.0, min(ceiling, float(speed_multiplier)))
    _state["speed_multiplier"] = clamped
    _state["target_mps"] = float(target_mps)
    logger.info("Speed set  multiplier=%.2f  target=%.2f m/s", clamped, target_mps)
    return {"speed_multiplier": clamped, "max_speed_mps": settings.max_speed_mps}


# ── Autonomous Patrol Loop (explicit lifecycle, safety-gated) ────────────────

_patrol_thread: threading.Thread | None = None
_patrol_stop_event = threading.Event()
_last_patrol_cmd = None


def _patrol_loop():
    global _last_patrol_cmd
    logger.info("Patrol loop thread started (runs only while mode=PATROL)")
    while not _patrol_stop_event.is_set():
        st = get_robot_state()
        # Phase 7: when a ROUTE session is active, the patrol engine drives —
        # this legacy wander loop must NOT fight it.
        try:
            from services.patrol_service import route_session_active
            if route_session_active():
                _last_patrol_cmd = None
                time.sleep(core_config.PATROL_TICK_S)
                continue
        except ImportError:
            pass  # patrol_service not available (should not happen)

        if st.get_mode() == PATROL and not st.is_estop_active():
            try:
                from services.telemetry_service import _sim
                front = _sim["ultrasonic"]["front_distance_m"]
                rear = _sim["ultrasonic"]["rear_distance_m"]

                if front < get_safety_config().get("patrol_obstacle_m"):
                    # SAFETY RULE: obstacle during autonomy → STOP first.
                    cmd = (0.0, 0.0, "STOP")
                else:
                    cmd = (0.5, 0.0, "FORWARD")

                if cmd != _last_patrol_cmd:
                    logger.info("Patrol logic: %s (Front: %sm, Rear: %sm)",
                                cmd[2], front, rear)
                    _last_patrol_cmd = cmd

                if cmd[2] == "STOP":
                    stop_all("patrol_service")
                else:
                    mult = _state["speed_multiplier"]
                    decision = apply_wheel_speeds(
                        cmd[0] * 100 * mult, cmd[1] * 100 * mult,
                        owner="patrol_service", mode=PATROL,
                    )
                    get_safety_layer().note_command("patrol_service")
                    if not decision.get("applied") and decision.get("reason") == "obstacle":
                        # Safety layer says stop — comply and log once
                        logger.warning("Patrol halted by safety layer: %s", decision)
                        _last_patrol_cmd = (0.0, 0.0, "SAFETY_STOP")
            except Exception as e:
                logger.error(f"Patrol loop error: {e}")
        else:
            _last_patrol_cmd = None

        time.sleep(core_config.PATROL_TICK_S)  # 2 Hz decisions


def start_patrol_loop() -> None:
    """Start the patrol thread (idempotent). Called from lifespan or set_mode(PATROL)."""
    global _patrol_thread
    if _patrol_thread is not None and _patrol_thread.is_alive():
        return
    _patrol_stop_event.clear()
    _patrol_thread = threading.Thread(target=_patrol_loop,
                                      name="sentra-patrol", daemon=True)
    _patrol_thread.start()


def stop_patrol_loop() -> None:
    """Signal the patrol thread to exit (it also idles when mode != PATROL)."""
    _patrol_stop_event.set()
