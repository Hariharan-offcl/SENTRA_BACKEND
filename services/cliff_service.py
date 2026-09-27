"""
SENTRA — IR cliff sensor service (Phase 3).

Reads the IR cliff sensors into the unified sensor state. Digital IR modules
(e.g. TCRT5000 comparator boards) drive their pin HIGH or LOW when an edge /
drop-off is detected — the active level is configurable per sensor.

Env vars (all optional):
    SENTRA_CLIFF_ENABLED   (default true)
    SENTRA_CLIFF_LEFT_GPIO / SENTRA_CLIFF_RIGHT_GPIO   (default 19 / 26 — not
                           yet wired on hardware; adjust to your build)
    SENTRA_CLIFF_ACTIVE_HIGH  (default true: 'cliff detected' = pin reads HIGH)

Simulation: when lgpio is absent or init fails, the service keeps a clean
False (no cliff) and marks itself simulated — dev machines and the Flutter
emulator path stay fully functional. No import-time GPIO side effects.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from core import config as core_config

logger = logging.getLogger(__name__)

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

CLIFF_ENABLED = os.getenv("SENTRA_CLIFF_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
CLIFF_LEFT_GPIO = int(os.getenv("SENTRA_CLIFF_LEFT_GPIO", "19"))
CLIFF_RIGHT_GPIO = int(os.getenv("SENTRA_CLIFF_RIGHT_GPIO", "26"))
CLIFF_ACTIVE_HIGH = os.getenv("SENTRA_CLIFF_ACTIVE_HIGH", "true").strip().lower() in (
    "1", "true", "yes", "on")
POLL_INTERVAL_S = 0.1

_h = None
_h_owned = False          # True if we opened our own chip handle
_simulated = not (CLIFF_ENABLED and _LGPIO_AVAILABLE)
_state = {
    "left_cliff": False,
    "right_cliff": False,
    "simulated": _simulated,
    "enabled": CLIFF_ENABLED,
    "last_read": 0.0,
}
_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _init_hardware() -> None:
    """Claim cliff pins. Prefers the motor service's shared chip handle."""
    global _h, _h_owned, _simulated
    if not CLIFF_ENABLED or not _LGPIO_AVAILABLE:
        return
    try:
        from services.motor_service import _h as motor_h
        if motor_h is not None:
            _h = motor_h
            _h_owned = False
        else:
            _h = lgpio.gpiochip_open(4)
            _h_owned = True
        for pin in (CLIFF_LEFT_GPIO, CLIFF_RIGHT_GPIO):
            lgpio.gpio_claim_input(_h, pin)
        _simulated = False
        logger.info("Cliff sensors initialized on GPIO %d/%d (active-%s)",
                    CLIFF_LEFT_GPIO, CLIFF_RIGHT_GPIO,
                    "high" if CLIFF_ACTIVE_HIGH else "low")
    except Exception as exc:
        logger.warning("Cliff hardware init failed — simulated (%s)", exc)
        _h = None
        _h_owned = False
        _simulated = True


def _read_once() -> tuple[bool, bool]:
    if _h is None:
        return False, False  # simulated: never a cliff
    try:
        left_raw = lgpio.gpio_read(_h, CLIFF_LEFT_GPIO)
        right_raw = lgpio.gpio_read(_h, CLIFF_RIGHT_GPIO)
        if CLIFF_ACTIVE_HIGH:
            return bool(left_raw), bool(right_raw)
        return not left_raw, not right_raw
    except Exception as exc:
        logger.error("Cliff read error: %s", exc)
        return False, False


def _poll_loop() -> None:
    while not _stop_event.is_set():
        left, right = _read_once()
        with _lock:
            _state["left_cliff"] = left
            _state["right_cliff"] = right
            _state["last_read"] = time.time()
        # Mirror into the unified telemetry state (WS telemetry shows live values)
        try:
            from services.telemetry_service import _sim
            _sim["cliff"]["left_detected"] = left
            _sim["cliff"]["right_detected"] = right
        except Exception:
            pass
        _stop_event.wait(POLL_INTERVAL_S)


def start_monitoring() -> None:
    """Idempotent. Call from lifespan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not CLIFF_ENABLED:
        logger.info("Cliff sensors disabled by config")
        return
    _init_hardware()
    _stop_event.clear()
    _thread = threading.Thread(target=_poll_loop, name="sentra-cliff", daemon=True)
    _thread.start()
    if _simulated:
        logger.info("Cliff service running in SIMULATED mode (no cliff GPIO)")


def stop_monitoring() -> None:
    _stop_event.set()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def sensor_provider() -> dict:
    """Provider fragment for the safety layer's unified sensor snapshot."""
    st = get_state()
    return {"left_cliff": st["left_cliff"], "right_cliff": st["right_cliff"]}
