"""
SENTRA — Motor / PWM abstraction layer.
Integrated with lgpio for Raspberry Pi 5.
"""

import logging
import atexit

logger = logging.getLogger(__name__)

# Try to load lgpio (will fail on Windows, so we wrap it)
try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False
    logger.warning("lgpio not found. Motor commands will be simulated (dev mode).")

# ── Hardware Pin Configuration (from harware/motor_driver.py) ───────────────
# Left Motor (A)
ENA, IN1, IN2 = 12, 17, 27
# Right Motor (B) - Reverted to exact match of ROS2 hardware file
ENB, IN3, IN4 = 13, 22, 23

# Shared in-memory state
_state = {
    "estop_active": False,
    "motors_disabled": False,
    "mode": "STANDBY",
    "speed_multiplier": 0.5,
    "target_mps": 0.0,
}

_h = None

def _init_hardware():
    global _h
    if not _LGPIO_AVAILABLE:
        return
    try:
        _h = lgpio.gpiochip_open(4) # Pi 5 main GPIO chip
        for pin in [ENA, IN1, IN2, ENB, IN3, IN4]:
            lgpio.gpio_claim_output(_h, pin, 0)
        logger.info("SENTRA motor hardware initialized on Pi 5 (lgpio)")
    except Exception as e:
        logger.error(f"Failed to initialize lgpio: {e}")
        _h = None

def _cleanup_hardware():
    if _h is not None:
        _set_motor(ENA, IN1, IN2, 0)
        _set_motor(ENB, IN3, IN4, 0)
        lgpio.gpiochip_close(_h)

# Initialize on startup, clean up on shutdown
_init_hardware()
atexit.register(_cleanup_hardware)

def get_state() -> dict:
    return dict(_state)

def _set_motor(en, a, b, speed_pct):
    """Low-level motor control (speed_pct: -100 to 100)"""
    if _h is None:
        return # Dev mode

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

# ── Motor primitives ─────────────────────────────────────────────────────────

def apply_locomotion(linear_velocity: float, angular_velocity: float, direction: str) -> None:
    if _state["estop_active"]:
        logger.warning("Locomotion command ignored — E-STOP is active")
        return
        
    logger.info("Motor CMD  dir=%s  linear=%.3f  angular=%.3f", direction, linear_velocity, angular_velocity)

    # Calculate differential drive
    left = linear_velocity - angular_velocity
    right = linear_velocity + angular_velocity

    # Clamp to -1.0 to 1.0
    left = max(-1.0, min(1.0, left))
    right = max(-1.0, min(1.0, right))

    # Convert to percentages for PWM (-100 to 100)
    # Scale by the global speed multiplier
    left_pct = left * 100 * _state["speed_multiplier"]
    right_pct = right * 100 * _state["speed_multiplier"]

    if direction == "STOP":
        left_pct = 0.0
        right_pct = 0.0

    _set_motor(ENA, IN1, IN2, left_pct)
    _set_motor(ENB, IN3, IN4, right_pct)

def trigger_estop() -> dict:
    _state["estop_active"] = True
    _state["motors_disabled"] = True
    logger.critical("E-STOP ACTIVATED — all motors disabled")
    _set_motor(ENA, IN1, IN2, 0)
    _set_motor(ENB, IN3, IN4, 0)
    return {"estop_active": True, "motors_disabled": True, "status": "E-STOP ACTIVATED"}

def reset_estop() -> dict:
    _state["estop_active"] = False
    _state["motors_disabled"] = False
    logger.info("E-STOP reset — motors ready")
    return {"estop_active": False, "motors_disabled": False, "status": "READY"}

def set_mode(mode: str) -> dict:
    _state["mode"] = mode
    logger.info("Robot mode → %s", mode)
    return {"active_mode": mode, "status": "READY"}

def set_speed(speed_multiplier: float, target_mps: float) -> dict:
    _state["speed_multiplier"] = speed_multiplier
    _state["target_mps"] = target_mps
    logger.info("Speed set  multiplier=%.2f  target=%.2f m/s", speed_multiplier, target_mps)
    return {"speed_multiplier": speed_multiplier, "max_speed_mps": 1.2}

