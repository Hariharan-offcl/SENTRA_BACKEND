"""
SENTRA — Phase 4 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase4_sensors.py

Covers: IMU service (simulated fallback, provider fragment), encoder service
(reset, duty-derived simulation reacting to drive commands), unified sensor
service (snapshot, health map, safety provider), telemetry enrichment, REST
endpoint, and app route registration.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


print("\n[1] imu_service — simulated fallback + provider fragment")
from services import imu_service

imu_service.start_monitoring()
time.sleep(0.4)
st = imu_service.get_state()
check("simulated on dev machine", st["simulated"] is True, str(st))
check("angles stay clean zeros in sim", st["pitch"] == 0.0 and st["roll"] == 0.0)
check("timestamp advancing", st["last_read"] > 0)
frag = imu_service.sensor_provider()
check("provider fragment keys", {"imu", "imu_simulated", "imu_enabled", "imu_last_read"} <= set(frag.keys()))
check("imu dict has axes", {"pitch", "roll", "yaw", "gx", "gz"} <= set(frag["imu"].keys()))

print("\n[2] encoder_service — duty-derived simulation")
from services import encoder_service

encoder_service.start_monitoring()
time.sleep(0.3)
enc = encoder_service.get_state()
check("simulated mode", enc["simulated"] is True)
check("starts at zero distance", enc["distance_m"]["left_m"] == 0.0, str(enc["distance_m"]))

# Drive forward via motion controller → simulated wheels should turn.
# First wire the safety singleton (unwired gate correctly refuses to move).
from core.safety import SafetyLayer
import core.safety as core_safety
sensors_dict = {"front_distance_m": 2.0, "rear_distance_m": 2.0,
                "left_cliff": False, "right_cliff": False}
sl_main = SafetyLayer()
sl_main.wire(motor_apply=lambda l, r: None, sensor_provider=lambda: dict(sensors_dict))
sl_main.wire_event_reporter(lambda *a, **k: None)
core_safety._safety = sl_main

from services import motor_service as ms
ms._h = None  # ensure dev mode
from core.state import get_robot_state, MANUAL
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
mc.start()
rs = get_robot_state()
rs.reset_estop()
r = mc.set_wheel_target(40, 40, source="test_ws")
check("drive command accepted", r.get("applied") is True, str(r))
# Check within the 0.6 s manual command-timeout window — after that the
# Phase 2 safety feature correctly auto-stops the silent 'source'.
time.sleep(0.3)
enc = encoder_service.get_state()
check("simulated wheels turning (duty-derived)",
      enc["speed_mps"]["left_mps"] > 0.0, str(enc["speed_mps"]))
time.sleep(0.4)
enc = encoder_service.get_state()
check("distance accumulating", enc["distance_m"]["left_m"] > 0.0, str(enc["distance_m"]))

mc.stop("test_ws")
time.sleep(0.5)
enc = encoder_service.get_state()
check("wheels stop after stop command", enc["speed_mps"]["left_mps"] == 0.0, str(enc["speed_mps"]))

reset_state = encoder_service.reset_odometry()
check("odometry reset works", reset_state["distance_m"]["left_m"] == 0.0
      and reset_state["ticks"]["left"] == 0)

frag = encoder_service.sensor_provider()
check("encoder fragment keys", {"wheel_encoders", "encoders_simulated",
                                "encoders_enabled", "encoders_last_read"} <= set(frag.keys()))

print("\n[3] sensor_service — unified snapshot + health")
from services import sensor_service

# aggregator starts with per-sensor services already running
sensor_service.start()
time.sleep(0.5)
snap = sensor_service.get_snapshot()
check("snapshot has distances", snap["front_distance_m"] == 1.2 and snap["rear_distance_m"] == 0.85, str(snap))
check("snapshot has cliff flags", "left_cliff" in snap and "right_cliff" in snap)
check("snapshot has imu dict", isinstance(snap["imu"], dict) and "pitch" in snap["imu"])
check("snapshot has encoders", "speed_mps" in snap["wheel_encoders"])
check("snapshot timestamp set", snap["timestamp"] > 0)

health = sensor_service.get_health()
check("health covers all four sensors",
      {"ultrasonic", "cliff", "imu", "wheel_encoders"} <= set(health.keys()), str(health.keys()))
check("ultrasonic marked STALE in sim (no hardware writes)", 
      health["ultrasonic"]["status"] in ("STALE", "SIMULATED", "NO_DATA"), str(health["ultrasonic"]))
check("imu marked SIMULATED", health["imu"]["status"] == "SIMULATED", str(health["imu"]))
check("encoders marked SIMULATED", health["wheel_encoders"]["status"] == "SIMULATED", str(health["wheel_encoders"]))

provider = sensor_service.sensor_provider()
check("safety provider shape", {"front_distance_m", "rear_distance_m", "left_cliff", "right_cliff"} <= set(provider.keys()))
check("provider serves distances", provider["front_distance_m"] == 1.2)

print("\n[4] safety gate still works through unified provider")
import core.state as core_state
from core.safety import SafetyLayer
import core.safety as core_safety

rs = core_state._robot_state
rs.reset_estop()

applied = []
sl = SafetyLayer()
sl.wire(motor_apply=lambda l, r: applied.append((l, r)),
        sensor_provider=lambda: dict(sensors_dict))
sl.wire_event_reporter(lambda *a, **k: None)
core_safety._safety = sl

if rs.get_mode() != MANUAL:
    rs.request_mode(MANUAL, requested_by="tester")
elif rs.get_mode_owner() != "tester":
    rs.request_mode(MANUAL, requested_by="tester")

sensors_dict["front_distance_m"] = 0.2
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("gate blocks at close obstacle via unified provider", d["applied"] is False, str(d))
sensors_dict["front_distance_m"] = 2.0
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("gate allows at clear path", d["applied"] is True, str(d))

print("\n[5] telemetry enrichment")
from services.telemetry_service import get_ws_telemetry_payload

payload = get_ws_telemetry_payload()
check("legacy keys intact", {"battery", "mode", "ultrasonic", "cliff", "mpu6050"} <= set(payload.keys()))
check("mpu6050 enriched from live imu", payload["mpu6050"].get("ax") is not None, str(payload["mpu6050"]))
check("wheel_encoders present", "wheel_encoders" in payload, str(payload.keys()))
check("ultrasonic legacy shape preserved (no last_read leaked)",
      set(payload["ultrasonic"].keys()) == {"front_distance_m", "rear_distance_m"}, str(payload["ultrasonic"]))

print("\n[6] REST endpoint + app integration")
from routers.telemetry import unified_sensors

resp = unified_sensors()
check("sensors endpoint returns snapshot+health", "sensors" in resp and "health" in resp)

from main import app
schema_paths = set(app.openapi()["paths"].keys())
check("/telemetry/sensors registered", "/api/v1/telemetry/sensors" in schema_paths)
check("earlier endpoints intact", {"/api/v1/safety/status", "/api/v1/control/move"} <= schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
