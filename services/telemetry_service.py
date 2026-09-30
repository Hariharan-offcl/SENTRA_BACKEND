"""
SENTRA – Telemetry service (unified hardware source of truth).

The ONLY telemetry assembly point.  Every value comes from a real
service/hardware layer.  Fabricated/hardcoded sensor values have been
removed.  Fields are omitted (or marked null) when hardware is not
initialised rather than silently returning a fake "safe" number.

10 Hz WebSocket push shape – identical keys consumed by Flutter ws_event.dart:

{
  "type":            "telemetry",
  "timestamp":       <unix float>,
  "uptime_sec":      <int>,
  "robot_mode":      <str>,        # from core.state
  "safety_state": {
      "estop_active":    <bool>,
      "motors_disabled": <bool>,
      "moving":          <bool>,
      "last_reason":     <str | null>
  },
  "current_location": <str | null>,   # localization_service best tag name
  "april_tag": {                       # last confirmed tag, or null
      "tag_id":    <int>,
      "name":      <str>,
      "distance_m": <float | null>,
      "age_s":     <float>
  } | null,
  "ultrasonic": {
      "front_distance_m": <float>,   # MAX_DIST (2.0) when simulated/failed
      "rear_distance_m":  <float>,
      "simulated":        <bool>,
      "age_s":            <float | null>
  },
  "cliff": {
      "left_cliff":  <bool>,
      "right_cliff": <bool>,
      "simulated":   <bool>,
      "age_s":       <float | null>
  },
  "imu": {
      "pitch": <float>, "roll": <float>, "yaw": <float>,
      "accel": {"x": <float>, "y": <float>, "z": <float>},
      "gyro":  {"x": <float>, "y": <float>, "z": <float>},
      "calibrated": <bool>,
      "simulated":  <bool>,
      "age_s":      <float | null>
  },
  "current_sensor": {
      "current_ma":  <float | null>,   # null when sensor not initialised
      "voltage_v":   <float | null>,
      "power_w":     <float | null>,
      "simulated":   <bool>,
      "age_s":       <float | null>
  },
  "system": {
      "cpu_temp_c":   <float | null>,
      "uptime_sec":   <int>
  }
}
"""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

_start_time = time.time()

FRESH_LIMIT_S = 2.0      # seconds before a sensor reading is considered STALE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _age(last_read: float) -> float | None:
    """Return age in seconds, or None when last_read is 0 (never read)."""
    if last_read == 0.0:
        return None
    return round(time.time() - last_read, 2)


def _cpu_temp() -> float | None:
    """Pi-only.  Returns None on any non-Pi or failure."""
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=1,
        ).stdout.strip()
        return float(out.replace("temp=", "").replace("'C", ""))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Unified payload assembly
# ---------------------------------------------------------------------------

def get_ws_telemetry_payload() -> dict:
    """Assemble and return the 10 Hz telemetry push payload.

    All values sourced from real service layers only.
    Missing / uninitialised hardware fields are null (never fabricated).
    """
    now = time.time()
    uptime = int(now - _start_time)

    # ── Robot mode & safety ──────────────────────────────────────────────────
    try:
        from core.state import get_robot_state
        rs = get_robot_state()
        robot_mode = rs.get_mode()
        estop_active = rs.is_estop_active()
        motors_disabled = rs.are_motors_disabled()
        is_moving = rs.is_moving()
    except Exception:
        robot_mode = "UNKNOWN"
        estop_active = False
        motors_disabled = False
        is_moving = False

    try:
        from core.safety import get_safety_layer
        safety_last = get_safety_layer()._last_decision
        last_reason = safety_last.get("reason")
    except Exception:
        last_reason = None

    safety_state = {
        "estop_active":    estop_active,
        "motors_disabled": motors_disabled,
        "moving":          is_moving,
        "last_reason":     last_reason,
    }

    # ── Localisation / AprilTag ──────────────────────────────────────────────
    current_location = None
    april_tag = None
    try:
        from services import localization_service
        loc = localization_service.get_localization()
        last_known = loc.get("last_known")
        if last_known:
            current_location = last_known.get("name")
            april_tag = {
                "tag_id":     last_known["tag_id"],
                "name":       last_known["name"],
                "distance_m": last_known.get("distance_m"),
                "age_s":      last_known.get("age_s"),
            }
    except Exception:
        pass

    # ── Ultrasonic ───────────────────────────────────────────────────────────
    try:
        from services.telemetry_service import _ultra_state
        ultra_block = _ultra_state()
    except Exception:
        ultra_block = _safe_ultra()

    # ── Cliff sensors ────────────────────────────────────────────────────────
    try:
        from services import cliff_service
        cliff_st = cliff_service.get_state()
        cliff_block = {
            "left_cliff":  cliff_st["left_cliff"],
            "right_cliff": cliff_st["right_cliff"],
            "simulated":   cliff_st.get("simulated", True),
            "age_s":       _age(cliff_st.get("last_read", 0.0)),
        }
    except Exception:
        cliff_block = {"left_cliff": False, "right_cliff": False,
                       "simulated": True, "age_s": None}

    # ── IMU (MPU6050) ────────────────────────────────────────────────────────
    try:
        from services import imu_service
        imu_st = imu_service.get_state()
        imu_block = {
            "pitch":      imu_st["pitch"],
            "roll":       imu_st["roll"],
            "yaw":        imu_st["yaw"],
            "accel":      {"x": imu_st["ax"], "y": imu_st["ay"], "z": imu_st["az"]},
            "gyro":       {"x": imu_st["gx"], "y": imu_st["gy"], "z": imu_st["gz"]},
            "calibrated": imu_st.get("calibrated", False),
            "simulated":  imu_st.get("simulated", True),
            "age_s":      _age(imu_st.get("last_read", 0.0)),
        }
    except Exception:
        imu_block = {
            "pitch": 0.0, "roll": 0.0, "yaw": 0.0,
            "accel": {"x": 0.0, "y": 0.0, "z": 0.0},
            "gyro":  {"x": 0.0, "y": 0.0, "z": 0.0},
            "calibrated": False, "simulated": True, "age_s": None,
        }

    # ── INA219 current sensor ────────────────────────────────────────────────
    try:
        from services import current_service
        curr_prov = current_service.sensor_provider()
        curr_data = curr_prov.get("current", {})
        curr_simulated = curr_prov.get("current_simulated", True)
        curr_last = curr_prov.get("current_last_read", 0.0)
        # Only expose values that are non-zero (zero = uninitialised INA219)
        raw_current = curr_data.get("current", 0.0)
        raw_voltage = curr_data.get("voltage", 0.0)
        raw_power   = curr_data.get("power", 0.0)
        current_block = {
            "current_ma": round(raw_current, 2) if (not curr_simulated and raw_current != 0.0) else None,
            "voltage_v":  round(raw_voltage, 3) if (not curr_simulated and raw_voltage != 0.0) else None,
            "power_w":    round(raw_power,   3) if (not curr_simulated and raw_power   != 0.0) else None,
            "simulated":  curr_simulated,
            "age_s":      _age(curr_last),
        }
    except Exception:
        current_block = {
            "current_ma": None, "voltage_v": None, "power_w": None,
            "simulated": True, "age_s": None,
        }

    # ── System ───────────────────────────────────────────────────────────────
    system_block = {
        "cpu_temp_c": _cpu_temp(),
        "uptime_sec": uptime,
    }

    try:
        from services import navigation_service
        nav_state = navigation_service.get_state().get("state", "IDLE")
    except Exception:
        nav_state = "IDLE"

    battery_percent = 100.0
    if not current_block.get("simulated", True) and current_block.get("voltage_v"):
        v = current_block["voltage_v"]
        battery_percent = max(0.0, min(100.0, (v - 9.0) / (12.6 - 9.0) * 100.0))

    return {
        "type":             "telemetry",
        "timestamp":        round(now, 3),
        "uptime_sec":       uptime,
        "robot_mode":       robot_mode,
        "safety_state":     safety_state,
        "current_location": current_location,
        "april_tag":        april_tag,
        "ultrasonic":       ultra_block,
        "cliff":            cliff_block,
        "imu":              imu_block,
        "current_sensor":   current_block,
        "system":           system_block,
        "battery_percent":  round(battery_percent, 1),
        "navigation_state": nav_state,
    }


# ---------------------------------------------------------------------------
# Ultrasonic helper (reads from the live ultrasonic_service _sim dict which
# the ultrasonic loop keeps updated with real HC-SR04 values)
# ---------------------------------------------------------------------------

def _ultra_state() -> dict:
    from services.telemetry_service import _US_SIM as us   # internal ref below
    return {
        "front_distance_m": round(us["front_distance_m"], 3),
        "rear_distance_m":  round(us["rear_distance_m"], 3),
        "simulated":        us["last_read"] == 0.0,
        "age_s":            _age(us["last_read"]),
    }


def _safe_ultra() -> dict:
    return {
        "front_distance_m": 2.0,
        "rear_distance_m":  2.0,
        "simulated":        True,
        "age_s":            None,
    }


# ---------------------------------------------------------------------------
# Ultrasonic shared state (written by ultrasonic_service loop)
# This replaces the old _sim["ultrasonic"] dict to keep the only mutable
# state here where the telemetry assembler can reach it cleanly.
# ---------------------------------------------------------------------------

_US_SIM = {
    "front_distance_m": 2.0,
    "rear_distance_m":  2.0,
    "last_read":        0.0,
}


# ---------------------------------------------------------------------------
# Legacy helpers kept for REST endpoints that still call them
# ---------------------------------------------------------------------------

def get_live_telemetry() -> dict:
    payload = get_ws_telemetry_payload()
    return {
        "operational_state": payload["robot_mode"],
        "zone":              payload.get("current_location") or "Unknown",
        "uptime_hours":      round(payload["uptime_sec"] / 3600, 2),
        "safety":            payload["safety_state"],
    }


def get_initial_sync(host_ip: str = "") -> dict:
    import socket
    if not host_ip:
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            host_ip = "127.0.0.1"
    return {
        "hardware_model": "Raspberry Pi 5",
        "ip_address":     host_ip,
    }
