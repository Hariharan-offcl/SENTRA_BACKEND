"""
SENTRA — System metrics service (Phase 17).

Real CPU / RAM / temperature / disk / load / uptime / process metrics for
`/api/v1/system/status`. Uses psutil, which works on both the dev machine
(Windows) and the target Raspberry Pi 5 (Linux). Every read is individually
guarded — a missing sensor or an exotic platform must never 500 the endpoint.

On the Pi, SoC temperature comes from `vcgencmd measure_temp` first (the
same path telemetry_service uses) with a psutil thermal-zone fallback, so
Windows dev boxes report a temperature of null instead of a fake value.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)

from core import simulation as _sim_mode  # Phase 18: mode info in every snapshot
from core import hardening as _hardening  # Phase 20: security findings

try:  # psutil is now pinned in requirements.txt (Phase 17)
    import psutil
except ImportError:  # pragma: no cover — defensive; psutil ships with the app
    psutil = None

_BOOT_TIME: float = psutil.boot_time() if psutil else time.time()

# Cached vcgencmd availability (probe once; vcgencmd only exists on the Pi).
_vcgencmd_ok: bool | None = None

# Last good temperature reading (some thermal reads fail transiently).
_last_temp_c: float | None = None


def _read_temp_vcgencmd() -> float | None:
    """Pi-only: `vcgencmd measure_temp` → e.g. temp=47.8'C. Cached probe."""
    global _vcgencmd_ok
    if _vcgencmd_ok is False:
        return None
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode != 0:
            raise OSError(result.returncode)
        _vcgencmd_ok = True
        return float(result.stdout.strip().replace("temp=", "").replace("'C", ""))
    except Exception:
        _vcgencmd_ok = False
        return None


def _read_temp_psutil() -> float | None:
    """psutil thermal zones (Linux) → first value that looks like a CPU temp."""
    if not psutil:
        return None
    try:
        temps = psutil.sensors_temperatures() or {}
        for name, entries in temps.items():
            for entry in entries:
                label = (entry.label or "").lower()
                if any(k in label or k in name.lower()
                       for k in ("cpu", "soc", "cpu_thermal")):
                    if entry.current and entry.current > 0:
                        return float(entry.current)
        # Fallback: any thermal zone at all.
        for entries in temps.values():
            for entry in entries:
                if entry.current and entry.current > 0:
                    return float(entry.current)
    except Exception:
        pass
    return None


def read_soc_temperature() -> float | None:
    """SoC temperature in °C, or None when the platform has no readable sensor."""
    global _last_temp_c
    temp = _read_temp_vcgencmd()
    if temp is None:
        temp = _read_temp_psutil()
    if temp is not None:
        _last_temp_c = temp
    return _last_temp_c


def get_system_status() -> dict:
    """
    Full health snapshot for GET /api/v1/system/status.

    Shape (all fields always present; `null` = not measurable on this host):
    {
      "timestamp": epoch seconds,
      "uptime_s": int,
      "cpu": {"percent": 0-100 (1s sample), "count": int, "freq_mhz": float|null,
              "load_avg": [1, 5, 15] | null},
      "memory": {"total_mb", "used_mb", "percent"},
      "swap": {"total_mb", "used_mb", "percent"},
      "disk": {"total_gb", "used_gb", "percent", "free_gb"},
      "temperature_c": float | null,
      "process": {"rss_mb", "threads", "connections", "cpu_percent", "uptime_s"},
      "simulation": {"simulation", "env_var", "latched", "hardware_disabled",
                     "preflight_stop"},
      "security": {"checked_at_startup", "findings[]", "count", "clean"},
      "source": "psutil" | "simulated"
    }
    """
    now = time.time()

    if not psutil:  # pragma: no cover — only when psutil is genuinely absent
        return {
            "timestamp": int(now),
            "uptime_s": int(now - _BOOT_TIME),
            "cpu": {"percent": None, "count": os.cpu_count() or 1,
                    "freq_mhz": None, "load_avg": None},
            "memory": {"total_mb": None, "used_mb": None, "percent": None},
            "swap": {"total_mb": None, "used_mb": None, "percent": None},
            "disk": {"total_gb": None, "used_gb": None, "percent": None,
                     "free_gb": None},
            "temperature_c": None,
            "process": {"rss_mb": None, "threads": None, "connections": None,
                        "cpu_percent": None, "uptime_s": None},
            "simulation": _sim_mode.status(),
            "security": _hardening.summary(),
            "source": "simulated",
        }

    # ── CPU ───────────────────────────────────────────────────────────────────
    cpu_percent: float | None = None
    freq_mhz: float | None = None
    load_avg: list | None = None
    try:
        cpu_percent = psutil.cpu_percent(interval=0.2)  # short blocking sample
    except Exception:
        pass
    try:
        freq = psutil.cpu_freq()
        freq_mhz = round(freq.current, 1) if freq and freq.current else None
    except Exception:
        pass
    try:
        la = psutil.getloadavg()
        load_avg = [round(x, 2) for x in la]
    except (AttributeError, OSError):
        load_avg = None  # Windows has no load average

    # ── Memory / swap ────────────────────────────────────────────────────────
    mem = swap = {"total_mb": None, "used_mb": None, "percent": None}
    try:
        vm = psutil.virtual_memory()
        mem = {
            "total_mb": round(vm.total / 1048576, 1),
            "used_mb": round(vm.used / 1048576, 1),
            "percent": round(vm.percent, 1),
        }
    except Exception:
        pass
    try:
        sm = psutil.swap_memory()
        swap = {
            "total_mb": round(sm.total / 1048576, 1),
            "used_mb": round(sm.used / 1048576, 1),
            "percent": round(sm.percent, 1),
        }
    except Exception:
        pass

    # ── Disk (root / system partition) ───────────────────────────────────────
    disk = {"total_gb": None, "used_gb": None, "percent": None, "free_gb": None}
    try:
        du = psutil.disk_usage(os.path.abspath(os.sep))
        disk = {
            "total_gb": round(du.total / 1073741824, 2),
            "used_gb": round(du.used / 1073741824, 2),
            "percent": round(du.percent, 1),
            "free_gb": round(du.free / 1073741824, 2),
        }
    except Exception:
        pass

    # ── Temperature ──────────────────────────────────────────────────────────
    temperature_c = read_soc_temperature()

    # ── This process (the backend itself) ────────────────────────────────────
    proc = {"rss_mb": None, "threads": None, "connections": None,
            "cpu_percent": None, "uptime_s": None}
    try:
        me = psutil.Process()
        with me.oneshot():
            proc = {
                "rss_mb": round(me.memory_info().rss / 1048576, 1),
                "threads": me.num_threads(),
                "connections": len(me.net_connections(kind="inet")),
                "cpu_percent": me.cpu_percent(None),  # since last call
                "uptime_s": int(now - me.create_time()),
            }
    except Exception:
        pass

    return {
        "timestamp": int(now),
        "uptime_s": int(now - _BOOT_TIME),
        "cpu": {"percent": cpu_percent, "count": psutil.cpu_count() or 1,
                "freq_mhz": freq_mhz, "load_avg": load_avg},
        "memory": mem,
        "swap": swap,
        "disk": disk,
        "temperature_c": temperature_c,
        "process": proc,
        "simulation": _sim_mode.status(),
        "security": _hardening.summary(),
        "source": "psutil",
    }
