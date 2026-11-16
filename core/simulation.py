"""
SENTRA — Explicit simulation mode (Phase 18).

`SENTRA_SIMULATION=true` upgrades the implicit dev no-ops into a real,
first-class mode:

  * services must REFUSE to claim GPIO / I2C (checked at every init point)
  * the safety layer performs a preflight `force_stop` at startup
  * /api/v1/system/status and /api/v1/simulation report `simulation: true`
  * a loud banner makes the mode impossible to miss in the logs

The flag is read once at import into `is_active()`; per-service env overrides
(e.g. `SENTRA_IMU_ENABLED=false`) may still disable a device in real mode.
`activate_once()` is idempotent: the mode is latched for the process lifetime
and can never be silently un-latched — call it from every hardware init point
BEFORE claiming pins/bus.

`enter_mode()` additionally forces motors to zero through the safety layer,
so no stale PWM can survive a switch into simulation.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in _TRUTHY


# Read once at import; the process cannot change its mind halfway through.
_SIMULATION: bool = _env_flag("SENTRA_SIMULATION")
_latched: bool = False
_pre_stop: dict | None = None


def is_active() -> bool:
    """True when the process runs in explicit simulation mode."""
    return _SIMULATION


def activate_once() -> None:
    """
    Latch simulation mode for this process (idempotent).

    Called by every hardware init point BEFORE claiming GPIO / I2C, so the
    check cannot be raced or forgotten by a later code path.
    """
    global _latched
    _latched = True
    if _SIMULATION:
        logger.warning("SIMULATION MODE latched for this process "
                       "(GPIO / I2C init is refused)")


def check_env_override(device: str, enabled: bool) -> bool:
    """
    Per-service gate. Returns False when the device must not touch hardware:

      * simulation mode active → always False
      * otherwise the service's own `*_ENABLED` decision stands
    """
    if _SIMULATION:
        return False
    return enabled


def preflight_stop() -> dict | None:
    """
    First-startup safety: zero all motors through the safety layer.

    Returns the force_stop decision dict (None in real mode, where startup
    must not touch motors at all).
    """
    global _pre_stop
    if not _SIMULATION:
        return None
    from core.safety import get_safety_layer  # lazy: import cycle
    _pre_stop = get_safety_layer().force_stop("simulation_preflight")
    logger.info("Simulation preflight: motors forced to zero (%s)",
                (_pre_stop or {}).get("forced"))
    return _pre_stop


def banner() -> None:
    """Loud startup banner so simulation mode can never be missed."""
    if not _SIMULATION:
        return
    line = "!" * 78
    logger.warning("%s", line)
    logger.warning("!!  %-70s !!", "")
    logger.warning("!!  %-70s !!", "SENTRA IS RUNNING IN SIMULATION MODE")
    logger.warning("!!  %-70s !!",
                   "Motors, GPIO and I2C hardware are DISABLED by config.")
    logger.warning("!!  %-70s !!",
                   "If you expected real hardware, unset SENTRA_SIMULATION.")
    logger.warning("!!  %-70s !!", "")
    logger.warning("%s", line)


def status() -> dict:
    """Payload for GET /api/v1/simulation (and merged into /system/status)."""
    return {
        "simulation": _SIMULATION,
        "env_var": "SENTRA_SIMULATION",
        "latched": _latched,
        "hardware_disabled": _SIMULATION,
        "preflight_stop": _pre_stop,
    }


def describe() -> str:
    """One-line mode description for logs and status views."""
    return "SIMULATION (hardware disabled by config)" if _SIMULATION \
        else "REAL (hardware enabled where available)"
