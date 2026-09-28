"""
SENTRA — IMU service (Phase 4).

MPU6050 (accel + gyro) on the I2C bus, read from a background thread so the
FastAPI event loop is never blocked. Integrates a complementary filter into
pitch/roll/yaw and publishes into the unified sensor state.

Env vars (optional):
    SENTRA_IMU_ENABLED      (default true)
    SENTRA_IMU_BUS          (default 1  → /dev/i2c-1, standard on Pi 5)
    SENTRA_IMU_ADDR         (default 0x68)
    SENTRA_IMU_POLL_S       (default 0.05 → 20 Hz)

Simulation: if smbus2 / the chip is missing (dev machine, or wiring not done),
the service runs with clean zeros and `simulated: true` — everything else
works identically. No import-time hardware side effects.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

from core import config as core_config  # Phase 18

logger = logging.getLogger(__name__)

IMU_ENABLED = os.getenv("SENTRA_IMU_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
IMU_BUS = int(os.getenv("SENTRA_IMU_BUS", "1"))
IMU_ADDR = int(os.getenv("SENTRA_IMU_ADDR", "0x68"), 16)
IMU_POLL_S = float(os.getenv("SENTRA_IMU_POLL_S", "0.05"))

from core.simulation import activate_once  # Phase 18

# MPU6050 registers
_REG_PWR_MGMT_1 = 0x6B
_REG_ACCEL_XOUT_H = 0x3B
_REG_GYRO_XOUT_H = 0x43

_lock = threading.Lock()
_state = {
    "pitch": 0.0,           # degrees, complementary-filtered
    "roll": 0.0,
    "yaw": 0.0,             # degrees, gyro-integrated (drifts without mag)
    "ax": 0.0, "ay": 0.0, "az": 0.0,   # g
    "gx": 0.0, "gy": 0.0, "gz": 0.0,   # deg/s
    "temp_c": 0.0,
    "simulated": True,
    "enabled": IMU_ENABLED,
    "last_read": 0.0,
}
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_smbus = None


def _init_bus():
    """Open the I2C bus; returns None in simulation."""
    activate_once()  # Phase 18: simulation mode refuses I2C init
    if core_config.SIMULATION:
        logger.warning("IMU: simulation mode — I2C bus refused")
        return None
    try:
        from smbus2 import SMBus
        bus = SMBus(IMU_BUS)
        # Wake the MPU6050 (clear sleep bit)
        bus.write_byte_data(IMU_ADDR, _REG_PWR_MGMT_1, 0)
        # Probe: read WHO_AM_I (0x75), expect 0x68 (or compatible clones)
        who = bus.read_byte_data(IMU_ADDR, 0x75)
        if who not in (0x68, 0x69, 0x98):
            logger.warning("IMU WHO_AM_I=0x%02X unexpected — using it anyway", who)
        logger.info("MPU6050 detected on bus %d addr 0x%02X", IMU_BUS, IMU_ADDR)
        return bus
    except Exception as exc:
        logger.warning("IMU hardware unavailable — simulated (%s)", exc)
        return None


def _read_raw(bus):
    """One burst read of 14 bytes: accel(6) temp(2) gyro(6). Returns dict or None."""
    try:
        data = bus.read_i2c_block_data(IMU_ADDR, _REG_ACCEL_XOUT_H, 14)
    except Exception:
        return None

    def s16(h, l):
        v = (h << 8) | l
        return v - 65536 if v >= 0x8000 else v

    ax = s16(data[0], data[1]) / 16384.0   # ±2g default range
    ay = s16(data[2], data[3]) / 16384.0
    az = s16(data[4], data[5]) / 16384.0
    temp_raw = s16(data[6], data[7])
    temp = temp_raw / 340.0 + 36.53
    gx = s16(data[8], data[9]) / 131.0     # ±250 °/s default
    gy = s16(data[10], data[11]) / 131.0
    gz = s16(data[12], data[13]) / 131.0
    return {"ax": ax, "ay": ay, "az": az, "gx": gx, "gy": gy, "gz": gz, "temp_c": temp}


def _poll_loop() -> None:
    global _smbus
    bus = _init_bus()
    _smbus = bus
    simulated = bus is None
    with _lock:
        _state["simulated"] = simulated

    pitch = roll = yaw = 0.0
    last = time.time()
    while not _stop_event.is_set():
        now = time.time()
        dt = min(now - last, 0.2)
        last = now

        if bus is not None:
            raw = _read_raw(bus)
            if raw is None:
                # transient I2C failure — keep last angles, mark staleness by timestamp
                _stop_event.wait(IMU_POLL_S)
                continue
            ax, ay, az = raw["ax"], raw["ay"], raw["az"]
            gx, gy, gz = raw["gx"], raw["gy"], raw["gz"]
            temp = raw["temp_c"]
            # Accelerometer angles
            acc_pitch = math.degrees(math.atan2(ay, math.hypot(ax, az)))
            acc_roll = math.degrees(math.atan2(ax, math.hypot(ay, az)))
            # Complementary filter (96% gyro integration, 4% accel correction)
            pitch = 0.96 * (pitch + gx * dt) + 0.04 * acc_pitch
            roll = 0.96 * (roll + gy * dt) + 0.04 * acc_roll
            yaw = (yaw + gz * dt) % 360.0
            with _lock:
                _state.update({"pitch": round(pitch, 2), "roll": round(roll, 2),
                               "yaw": round(yaw, 2),
                               "ax": ax, "ay": ay, "az": az,
                               "gx": gx, "gy": gy, "gz": gz,
                               "temp_c": round(temp, 1),
                               "last_read": now})
        else:
            # Simulation: hold clean zeros; still advance timestamp
            with _lock:
                _state["last_read"] = now
        _stop_event.wait(IMU_POLL_S)


def start_monitoring() -> None:
    """Idempotent; call from lifespan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not IMU_ENABLED:
        logger.info("IMU disabled by config")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_poll_loop, name="sentra-imu", daemon=True)
    _thread.start()


def stop_monitoring() -> None:
    _stop_event.set()


def get_state() -> dict:
    with _lock:
        return dict(_state)


def sensor_provider() -> dict:
    """Fragment for the unified sensor aggregator."""
    st = get_state()
    return {
        "imu": {"pitch": st["pitch"], "roll": st["roll"], "yaw": st["yaw"],
                "ax": st["ax"], "ay": st["ay"], "az": st["az"],
                "gx": st["gx"], "gy": st["gy"], "gz": st["gz"],
                "temp_c": st["temp_c"]},
        "imu_simulated": st["simulated"],
        "imu_enabled": st["enabled"],
        "imu_last_read": st["last_read"],
    }
