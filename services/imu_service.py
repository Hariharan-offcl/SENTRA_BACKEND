"""
SENTRA — IMU service (Phase 5).

High-level service that wraps the Hardware Abstraction Layer (HAL) for IMU.
It provides a thread-safe state for the rest of the system and exposes
calibration controls.

The actual hardware logic (I2C, MPU6050 registers, bias compensation)
resides in hardware/imu/physical.py.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from core import config as core_config
from hardware.manager import hardware_manager

logger = logging.getLogger(__name__)

# Thread-safe cached state for telemetry/sensor aggregator
_lock = threading.Lock()
_state = {
    "pitch": 0.0,
    "roll": 0.0,
    "yaw": 0.0,
    "ax": 0.0, "ay": 0.0, "az": 0.0,
    "gx": 0.0, "gy": 0.0, "gz": 0.0,
    "simulated": True,
    "enabled": True,
    "last_read": 0.0,
    "calibrated": False,
}
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _init_bus() -> Optional[Any]:
    """Return the I2C bus handle, or None when it must not be touched.

    Simulation mode (Phase 18) always refuses. On a dev machine without
    smbus2/working I2C this stays None and the service keeps simulating.
    """
    if core_config.SIMULATION:
        return None
    try:
        from hardware.bus_manager import bus_manager
        return bus_manager.bus
    except Exception as exc:
        logger.debug("IMU I2C bus unavailable (%s)", exc)
        return None


def _poll_loop() -> None:
    """
    Background loop that keeps the _state cache updated from the HAL.
    """
    global _thread
    imu = hardware_manager.imu

    last = time.time()
    while not _stop_event.is_set():
        try:
            # Get current filtered data from HAL
            data = imu.get_data()

            with _lock:
                _state.update({
                    "pitch": data.get("pitch", 0.0),
                    "roll": data.get("roll", 0.0),
                    "yaw": data.get("heading", 0.0),
                    "ax": data["accel"]["x"],
                    "ay": data["accel"]["y"],
                    "az": data["accel"]["z"],
                    "gx": data["gyro"]["x"],
                    "gy": data["gyro"]["y"],
                    "gz": data["gyro"]["z"],
                    "last_read": time.time(),
                    "simulated": imu.status().get("mode") == "simulated",
                    "calibrated": imu.status().get("calibrated", False)
                })
        except Exception as e:
            logger.error("IMU service poll error: %s", e)

        time.sleep(0.05) # 20 Hz

def start_monitoring() -> None:
    """Idempotent; call from lifespan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_poll_loop, name="sentra-imu-svc", daemon=True)
    _thread.start()
    logger.info("IMU service monitoring started")

def stop_monitoring() -> None:
    _stop_event.set()

def calibrate() -> dict:
    """
    Triggers the hardware-level calibration.
    The robot must be stationary.
    """
    imu = hardware_manager.imu
    success = imu.calibrate()
    return {
        "ok": success,
        "message": "Calibration successful" if success else "Calibration failed"
    }

def get_state() -> dict:
    with _lock:
        return dict(_state)

def sensor_provider() -> dict:
    """Fragment for the unified sensor aggregator."""
    st = get_state()
    return {
        "imu": {
            "pitch": st["pitch"],
            "roll": st["roll"],
            "yaw": st["yaw"],
            "ax": st["ax"], "ay": st["ay"], "az": st["az"],
            "gx": st["gx"], "gy": st["gy"], "gz": st["gz"],
        },
        "imu_simulated": hardware_manager.imu.status().get("mode") == "simulated",
        "imu_enabled": st["enabled"],
        "imu_last_read": st["last_read"],
        "imu_calibrated": st["calibrated"],
    }
