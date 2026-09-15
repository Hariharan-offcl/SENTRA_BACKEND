"""
SENTRA — Motor / PWM abstraction layer.

On a real Raspberry Pi 5, swap the stub implementations below for
actual GPIO/PWM calls using gpiozero or RPi.GPIO.  The rest of the
backend never touches GPIO directly; it only calls this service.
"""

import logging

logger = logging.getLogger(__name__)

# Shared in-memory state ─────────────────────────────────────────────────────

_state = {
    "estop_active": False,
    "motors_disabled": False,
    "mode": "STANDBY",          # PATROL | MANUAL | STANDBY
    "speed_multiplier": 0.5,
    "target_mps": 0.0,
}


def get_state() -> dict:
    return dict(_state)


# ── Motor primitives ─────────────────────────────────────────────────────────

def apply_locomotion(linear_velocity: float, angular_velocity: float, direction: str) -> None:
    """
    Apply PWM signals to drive motors.
    Replace the log statement with real GPIO calls on the Pi:

        left_motor.value  = linear_velocity - angular_velocity
        right_motor.value = linear_velocity + angular_velocity
    """
    if _state["estop_active"]:
        logger.warning("Locomotion command ignored — E-STOP is active")
        return
    logger.info("Motor CMD  dir=%s  linear=%.3f  angular=%.3f", direction, linear_velocity, angular_velocity)


def trigger_estop() -> dict:
    """Kill all motor PWM signals immediately (< 5 ms target on real hardware)."""
    _state["estop_active"] = True
    _state["motors_disabled"] = True
    logger.critical("E-STOP ACTIVATED — all motors disabled")
    # On Pi:  left_motor.stop(); right_motor.stop()
    return {
        "estop_active": True,
        "motors_disabled": True,
        "status": "E-STOP ACTIVATED",
    }


def reset_estop() -> dict:
    """Clear the E-STOP latch so motors can be re-enabled."""
    _state["estop_active"] = False
    _state["motors_disabled"] = False
    logger.info("E-STOP reset — motors ready")
    return {
        "estop_active": False,
        "motors_disabled": False,
        "status": "READY",
    }


def set_mode(mode: str) -> dict:
    _state["mode"] = mode
    logger.info("Robot mode → %s", mode)
    return {"active_mode": mode, "status": "READY"}


def set_speed(speed_multiplier: float, target_mps: float) -> dict:
    _state["speed_multiplier"] = speed_multiplier
    _state["target_mps"] = target_mps
    logger.info("Speed set  multiplier=%.2f  target=%.2f m/s", speed_multiplier, target_mps)
    return {"speed_multiplier": speed_multiplier, "max_speed_mps": 1.2}
