"""
SENTRA — Unified sensor service (Phase 4).

One aggregated, health-tracked sensor snapshot built at 10 Hz from the
individual sensor services (ultrasonic, cliff, IMU, encoders). This is what
the safety gate, telemetry WS and future navigation/vision logic read.

Design rules:
    - All hardware reads happen in the sensor services' own daemon threads;
      this aggregator only assembles values (never blocks the event loop).
    - Every fragment carries a timestamp; the snapshot reports per-sensor
      health (FRESH / STALE / SIMULATED / DISABLED / NO_DATA).
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

FRESH_LIMIT_S = 1.5  # older than this = STALE

_lock = threading.RLock()
_snapshot: dict = {
    "front_distance_m": None,
    "rear_distance_m": None,
    "left_cliff": False,
    "right_cliff": False,
    "imu": {},
    "wheel_encoders": {},
    "timestamp": 0.0,
}
_health: dict = {}
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _age_of(last_read: float | None) -> float | None:
    if not last_read:
        return None
    return round(time.time() - last_read, 2)


def _health_of(*, enabled: bool, simulated: bool, last_read: float | None) -> dict:
    age = _age_of(last_read)
    if not enabled:
        status = "DISABLED"
    elif simulated:
        status = "SIMULATED"
    elif age is None:
        status = "NO_DATA"
    elif age > FRESH_LIMIT_S:
        status = "STALE"
    else:
        status = "FRESH"
    return {"status": status, "age_s": age}


def _build_once() -> None:
    from services import ultrasonic_service, cliff_service, imu_service, encoder_service
    from services.telemetry_service import _sim

    # Ultrasonic values live in telemetry _sim (existing write path).
    ultra_last = _sim["ultrasonic"].get("last_read")

    cliff_state = cliff_service.get_state()
    imu_frag = imu_service.sensor_provider()
    enc_frag = encoder_service.sensor_provider()

    snap = {
        "front_distance_m": _sim["ultrasonic"]["front_distance_m"],
        "rear_distance_m": _sim["ultrasonic"]["rear_distance_m"],
        "left_cliff": bool(cliff_state["left_cliff"]),
        "right_cliff": bool(cliff_state["right_cliff"]),
        "imu": imu_frag["imu"],
        "wheel_encoders": enc_frag["wheel_encoders"],
        "timestamp": time.time(),
    }

    health = {
        "ultrasonic": _health_of(enabled=True, simulated=False,
                                 last_read=ultra_last),
        "cliff": _health_of(enabled=cliff_state["enabled"],
                            simulated=cliff_state["simulated"],
                            last_read=cliff_state["last_read"]),
        "imu": _health_of(enabled=imu_frag["imu_enabled"],
                          simulated=imu_frag["imu_simulated"],
                          last_read=imu_frag["imu_last_read"]),
        "wheel_encoders": _health_of(enabled=enc_frag["encoders_enabled"],
                                     simulated=enc_frag["encoders_simulated"],
                                     last_read=enc_frag["encoders_last_read"]),
    }

    with _lock:
        _snapshot.clear()
        _snapshot.update(snap)
        _health.clear()
        _health.update(health)


def _loop() -> None:
    while not _stop_event.is_set():
        try:
            _build_once()
        except Exception as exc:
            logger.error("sensor aggregation error: %s", exc)
        _stop_event.wait(0.1)


def start() -> None:
    """Idempotent; call from lifespan AFTER the per-sensor services start."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="sentra-sensors", daemon=True)
    _thread.start()
    logger.info("Unified sensor aggregator started (10 Hz, freshness limit %.1fs)",
                FRESH_LIMIT_S)


def stop() -> None:
    _stop_event.set()


def get_snapshot() -> dict:
    with _lock:
        return dict(_snapshot)


def get_health() -> dict:
    with _lock:
        return dict(_health)


def sensor_provider() -> dict:
    """Provider for the safety gate: latest aggregated values. If the
    aggregator has not run yet, falls back to a direct per-sensor read so
    the safety gate is never without data (e.g. during startup)."""
    snap = get_snapshot()
    if snap["timestamp"] == 0.0:
        from services.telemetry_service import _sim
        from services import cliff_service
        frag = cliff_service.sensor_provider()
        return {
            "front_distance_m": _sim["ultrasonic"]["front_distance_m"],
            "rear_distance_m": _sim["ultrasonic"]["rear_distance_m"],
            "left_cliff": frag["left_cliff"],
            "right_cliff": frag["right_cliff"],
        }
    return {
        "front_distance_m": snap["front_distance_m"],
        "rear_distance_m": snap["rear_distance_m"],
        "left_cliff": bool(snap["left_cliff"]),
        "right_cliff": bool(snap["right_cliff"]),
    }
