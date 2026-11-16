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
_simulated = not (CLIFF_ENABLED and _LGPIO_AVAILABLE) or core_config.SIMULATION  # Phase 18
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
    """Link to the HAL's cliff sensor instead of claiming pins itself."""
    global _simulated
    from core.simulation import activate_once
    activate_once()
    
    if not CLIFF_ENABLED or core_config.SIMULATION:
        _simulated = True
        return
        
    try:
        from hardware.manager import hardware_manager
        cliff_hal = hardware_manager.cliff
        if cliff_hal is None or not getattr(cliff_hal, "initialized", False):
            raise RuntimeError("Cliff HAL not initialized")

        _simulated = False
        logger.info("Cliff service linked to HAL (shared handle)")
    except Exception as exc:
        logger.warning("Cliff service linkage failed — simulated (%s)", exc)
        _simulated = True


def _read_once() -> tuple[bool, bool]:
    if _simulated:
        return False, False  # simulated: never a cliff
        
    try:
        from hardware.manager import hardware_manager
        res = hardware_manager.cliff.read()
        return res.get("left", False), res.get("right", False)
    except Exception as exc:
        logger.error("Cliff read error: %s", exc)
        return False, False


def _poll_loop() -> None:
    while not _stop_event.is_set():
        left, right = _read_once()
        with _lock:
            _state["left_cliff"]  = left
            _state["right_cliff"] = right
            _state["simulated"]   = _simulated
            _state["last_read"]   = time.time()
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
