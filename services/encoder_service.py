"""
SENTRA — Wheel encoder service (Phase 4).

Counts quadrature/step ticks from the wheel encoders via lgpio callbacks and
integrates travelled distance + speed. On a dev machine (or before encoder
wiring) it falls back to a **duty-derived simulation** that reacts to real
drive commands — so mapping/patrol logic can be developed without the wheels
turning.

Env vars (optional):
    SENTRA_ENCODERS_ENABLED   (default true)
    SENTRA_ENCODER_LEFT_GPIO  (default 23 *reserved on Pi 5 — set to your pin*)
    SENTRA_ENCODER_RIGHT_GPIO (default 16)
    SENTRA_TICKS_PER_REV      (default 20 — counts per wheel revolution)
    SENTRA_WHEEL_DIAM_M       (default 0.065)

NOTE on defaults: GPIO 23 is used by IN4 on the motor driver; the default here
is a placeholder. Set SENTRA_ENCODER_LEFT_GPIO to your real wiring before
hardware use — until then the service refuses to claim pins that collide with
motor/ultrasonic/cliff pins and runs simulated.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

from core import config as core_config
from core.simulation import activate_once  # Phase 18

ENCODERS_ENABLED = os.getenv("SENTRA_ENCODERS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
ENCODER_LEFT_GPIO = int(os.getenv("SENTRA_ENCODER_LEFT_GPIO", "23"))
ENCODER_RIGHT_GPIO = int(os.getenv("SENTRA_ENCODER_RIGHT_GPIO", "16"))
TICKS_PER_REV = int(os.getenv("SENTRA_TICKS_PER_REV", "20"))
WHEEL_DIAM_M = float(os.getenv("SENTRA_WHEEL_DIAM_M", "0.065"))
M_PER_TICK = math.pi * WHEEL_DIAM_M / TICKS_PER_REV

# Pins owned elsewhere — never claim these as encoder inputs.
_RESERVED = {12, 27, 17, 13, 22, 23,   # motors
             24, 25, 5, 6,             # ultrasonic
             19, 26}                   # cliff defaults

_h = None
_h_owned = False
_simulated = True
_cb_left = None
_cb_right = None

_lock = threading.Lock()
_ticks = {"left": 0, "right": 0}
_speed = {"left_mps": 0.0, "right_mps": 0.0}
_distance = {"left_m": 0.0, "right_m": 0.0}
_last_tick_time = {"left": 0.0, "right": 0.0}
_state = {
    "simulated": _simulated,
    "enabled": ENCODERS_ENABLED,
    "last_read": 0.0,
}
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _pins_collide() -> bool:
    return ENCODER_LEFT_GPIO in _RESERVED or ENCODER_RIGHT_GPIO in _RESERVED


def _init_hardware() -> None:
    global _h, _h_owned, _simulated
    activate_once()  # Phase 18: simulation mode refuses GPIO init
    if not ENCODERS_ENABLED or not _LGPIO_AVAILABLE or core_config.SIMULATION:
        return
    if _pins_collide():
        logger.warning(
            "Encoder pins %d/%d collide with reserved pins — running SIMULATED. "
            "Set SENTRA_ENCODER_LEFT_GPIO / SENTRA_ENCODER_RIGHT_GPIO to your wiring.",
            ENCODER_LEFT_GPIO, ENCODER_RIGHT_GPIO)
        return
    try:
        from services.motor_service import _h as motor_h
        if motor_h is not None:
            _h = motor_h
            _h_owned = False
        else:
            _h = lgpio.gpiochip_open(4)
            _h_owned = True
        lgpio.gpio_claim_input(_h, ENCODER_LEFT_GPIO)
        lgpio.gpio_claim_input(_h, ENCODER_RIGHT_GPIO)
        global _cb_left, _cb_right
        _cb_left = lgpio.callback(_h, ENCODER_LEFT_GPIO, lgpio.BOTH_EDGES, _on_left_tick)
        _cb_right = lgpio.callback(_h, ENCODER_RIGHT_GPIO, lgpio.BOTH_EDGES, _on_right_tick)
        _simulated = False
        logger.info("Wheel encoders on GPIO %d/%d (%d ticks/rev, %.1f cm wheel)",
                    ENCODER_LEFT_GPIO, ENCODER_RIGHT_GPIO, TICKS_PER_REV, WHEEL_DIAM_M * 100)
    except Exception as exc:
        logger.warning("Encoder hardware init failed — simulated (%s)", exc)
        _h = None
        _h_owned = False
        _simulated = True


def _on_left_tick(chip, gpio, level, timestamp_us, _stamp=None):
    with _lock:
        _ticks["left"] += 1
        _last_tick_time["left"] = time.time()


def _on_right_tick(chip, gpio, level, timestamp_us, _stamp=None):
    with _lock:
        _ticks["right"] += 1
        _last_tick_time["right"] = time.time()


def _duty_sim_loop() -> None:
    """Simulation: derive wheel speed from the last commanded motor duty."""
    last = time.time()
    while not _stop_event.is_set():
        now = time.time()
        dt = min(now - last, 0.2)
        last = now
        try:
            from services.motion_controller import get_motion_controller
            mc = get_motion_controller()
            with mc._lock:
                left_duty = mc._current_left / 100.0
                right_duty = mc._current_right / 100.0
        except Exception:
            left_duty = right_duty = 0.0
        # Rough model: full duty ≈ 0.6 m/s at the current speed multiplier
        est_max_mps = 0.6
        with _lock:
            for side, duty in (("left", left_duty), ("right", right_duty)):
                v = duty * est_max_mps
                _speed[f"{side}_mps"] = round(v, 3)
                _distance[f"{side}_m"] += abs(v) * dt
            _state["last_read"] = now
        _stop_event.wait(0.1)


def _hardware_speed_loop() -> None:
    """Hardware: compute m/s and metres from tick deltas each 100 ms."""
    last = time.time()
    prev = {"left": 0, "right": 0}
    while not _stop_event.is_set():
        now = time.time()
        dt = min(now - last, 0.5)
        last = now
        with _lock:
            d_left = _ticks["left"] - prev["left"]
            d_right = _ticks["right"] - prev["right"]
            prev["left"] = _ticks["left"]
            prev["right"] = _ticks["right"]
            for side, dticks in (("left", d_left), ("right", d_right)):
                dist = dticks * M_PER_TICK
                _speed[f"{side}_mps"] = round(dist / dt if dt > 0 else 0.0, 3)
                _distance[f"{side}_m"] = round(_distance[f"{side}_m"] + dist, 4)
            _state["last_read"] = now
        _stop_event.wait(0.1)


def start_monitoring() -> None:
    """Idempotent; call from lifespan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not ENCODERS_ENABLED:
        logger.info("Wheel encoders disabled by config")
        return
    _init_hardware()
    _stop_event.clear()
    target = _hardware_speed_loop if not _simulated else _duty_sim_loop
    _thread = threading.Thread(target=target,
                               name="sentra-encoders", daemon=True)
    _thread.start()
    logger.info("Encoder service running in %s mode",
                "HARDWARE" if not _simulated else "SIMULATED (duty-derived)")


def stop_monitoring() -> None:
    _stop_event.set()


def get_state() -> dict:
    with _lock:
        return {
            "ticks": dict(_ticks),
            "speed_mps": dict(_speed),
            "distance_m": dict(_distance),
            **_state,
        }


def reset_odometry() -> dict:
    with _lock:
        _ticks["left"] = _ticks["right"] = 0
        _distance["left_m"] = _distance["right_m"] = 0.0
    logger.info("Odometry reset")
    return get_state()


def sensor_provider() -> dict:
    """Fragment for the unified sensor aggregator."""
    st = get_state()
    return {
        "wheel_encoders": {"ticks": st["ticks"], "speed_mps": st["speed_mps"],
                           "distance_m": st["distance_m"]},
        "encoders_simulated": st["simulated"],
        "encoders_enabled": st["enabled"],
        "encoders_last_read": st["last_read"],
    }
