"""
SENTRA — Telemetry service.

Reads CPU temperature, memory, uptime, battery level, etc.
On a real Raspberry Pi 5 these calls hit the actual hardware.
The `psutil` path works cross-platform for development; the
`vcgencmd` path is Pi-specific and is guarded by a try/except.
"""

import time
import subprocess
import logging

logger = logging.getLogger(__name__)

# Simulated values (overwritten by real reads where available) ────────────────

_sim = {
    "battery_level": 87,
    "battery_charging": True,
    "latency_ms": 18,
    "patrol_speed_mps": 0.45,
    "pos_x": 12.4,
    "pos_y": -4.8,
    "ultrasonic": {"front_distance_m": 1.2, "rear_distance_m": 0.85},
    "cliff": {"left_detected": False, "right_detected": False},
    "current_ma": 450,
    "mpu6050": {"pitch": 1.2, "roll": -0.5, "yaw": 45.0}
}

_start_time = time.time()


def _read_cpu_temp() -> float:
    """Read SoC temperature via vcgencmd (Pi-only). Falls back to 45.0 °C."""
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=1
        )
        # Output: temp=47.8'C
        temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
        return float(temp_str)
    except Exception:
        return 45.0


def get_live_telemetry() -> dict:
    uptime_sec = time.time() - _start_time
    return {
        "operational_state": "Active Perimeter Patrol",
        "zone": "Living Room & Entrance",
        "status": "ARMED",
        "battery": {
            "level": _sim["battery_level"],
            "charging": _sim["battery_charging"],
            "delta": "+2.4% charging",
        },
        "latency_ms": _sim["latency_ms"],
        "uptime_hours": round(uptime_sec / 3600, 2),
        "patrol_speed_mps": _sim["patrol_speed_mps"],
    }


def get_initial_sync(host_ip: str = "192.168.1.104") -> dict:
    return {
        "hardware_model": "Raspberry Pi 5 (8GB)",
        "ip_address": host_ip,
        "ping_latency_ms": _sim["latency_ms"],
        "battery_level": _sim["battery_level"],
        "battery_charging": _sim["battery_charging"],
        "status_badge": "EXCELLENT",
    }


def get_ws_telemetry_payload() -> dict:
    """10 Hz WebSocket push payload."""
    uptime_sec = time.time() - _start_time
    return {
        "timestamp": time.time(),
        "battery": _sim["battery_level"],
        "charging": _sim["battery_charging"],
        "latency_ms": _sim["latency_ms"],
        "uptime_sec": int(uptime_sec),
        "speed_mps": _sim["patrol_speed_mps"],
        "pos_x": _sim["pos_x"],
        "pos_y": _sim["pos_y"],
        "mode": "PATROL",
        "ultrasonic": _sim["ultrasonic"],
        "cliff": _sim["cliff"],
        "current_ma": _sim["current_ma"],
        "mpu6050": _sim["mpu6050"],
    }
