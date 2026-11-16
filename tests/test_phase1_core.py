"""
SENTRA — Phase 1 hardware-independent tests.

Run anywhere (no Pi, no lgpio needed):
    python -m pytest tests/test_phase1_core.py -v
or:
    python tests/test_phase1_core.py

Covers: central state machine, safety gating (estop / obstacle / timeout /
mode arbitration), motor service differential math, and API import integrity.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ── 1. Central state machine ─────────────────────────────────────────────────

print("\n[1] core.state — central robot state")
from core.state import (
    RobotState, get_robot_state, MANUAL, PATROL, NAVIGATION, STANDBY,
    EMERGENCY_STOP, ALL_MODES,
)

rs = RobotState()
check("initial mode is STANDBY", rs.get_mode() == STANDBY)
check("not moving initially", not rs.is_moving())

r = rs.request_mode(MANUAL, requested_by="test")
check("STANDBY → MANUAL accepted", r.get("accepted") is True, str(r))
check("mode owner set", rs.get_mode_owner() is not None)

r = rs.request_mode("WARP_DRIVE", requested_by="test")
check("unknown mode rejected", r.get("accepted") is False)

r = rs.request_mode(EMERGENCY_STOP, requested_by="test")
check("direct EMERGENCY_STOP request rejected", r.get("accepted") is False)

r = rs.request_mode(PATROL, requested_by="patrol_service")
check("MANUAL → PATROL accepted", r.get("accepted") is True, str(r))
check("owner updated to patrol_service", rs.get_mode_owner() == "patrol_service")

estop = rs.trigger_estop(reason="test")
check("estop latches", estop["estop_active"] is True)
check("mode becomes EMERGENCY_STOP", rs.get_mode() == EMERGENCY_STOP)

r = rs.request_mode(MANUAL, requested_by="test")
check("mode request while estopped rejected", r.get("accepted") is False)

reset = rs.reset_estop()
check("estop reset", reset["estop_active"] is False)
check("mode back to STANDBY after reset", rs.get_mode() == STANDBY)

snap = rs.snapshot()
check("snapshot has expected keys",
      {"mode", "estop_active", "motors_disabled", "moving"} <= set(snap.keys()))

# Singleton
check("get_robot_state returns singleton", get_robot_state() is get_robot_state())


# ── 2. Safety layer gating ───────────────────────────────────────────────────

print("\n[2] core.safety — motor command gate")
from core.safety import SafetyLayer, get_safety_layer
from core import config as core_config

applied_commands: list[tuple[float, float]] = []


def fake_motor_apply(left: float, right: float) -> None:
    applied_commands.append((left, right))


sensors: dict = {}


def fake_sensors() -> dict:
    return dict(sensors)


sl = SafetyLayer()
sl.wire(motor_apply=fake_motor_apply, sensor_provider=fake_sensors)
rs2 = RobotState()
import core.state as core_state
core_state._robot_state = rs2  # point singleton at fresh instance for this test

# not wired → already wired above; mode mismatch first
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("mode mismatch blocks motors", d["applied"] is False and d["reason"] == "mode_mismatch", str(d))

rs2.request_mode(MANUAL, requested_by="tester")

d = sl.check_and_apply("wrong_owner", MANUAL, 50, 50)
check("wrong owner blocked", d["applied"] is False and d["reason"] == "mode_mismatch", str(d))

d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("valid manual command applied", d["applied"] is True, str(d))
check("motor driver received duty", applied_commands and applied_commands[-1] == (50, 50))

# Obstacle stop — forward with close front obstacle
sensors["front_distance_m"] = 0.2
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("front obstacle blocks forward", d["applied"] is False and d["reason"] == "obstacle", str(d))
check("motors zeroed on obstacle", applied_commands[-1] == (0.0, 0.0))

# Rotation (not forward) is allowed past front obstacle
d = sl.check_and_apply("tester", MANUAL, -30, 30)
check("rotation allowed with front obstacle (no forward motion)",
      d["applied"] is True, str(d))

# Rear obstacle blocks backward
sensors["front_distance_m"] = 2.0
sensors["rear_distance_m"] = 0.2
d = sl.check_and_apply("tester", MANUAL, -50, -50)
check("rear obstacle blocks backward", d["applied"] is False and d["reason"] == "obstacle", str(d))

# Cliff stop
sensors["rear_distance_m"] = 2.0
sensors["left_cliff"] = True
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("cliff blocks movement", d["applied"] is False and d["reason"] == "obstacle", str(d))
sensors["left_cliff"] = False

# Speed clamp
d = sl.check_and_apply("tester", MANUAL, 500, -500)
check("duty clamped to ±100", d["applied"] is True and max(abs(d["left"]), abs(d["right"])) <= 100, str(d))

# Invalid direction
d = sl.check_and_apply("tester", MANUAL, float("nan"), 10)
check("NaN rejected", d["applied"] is False and d["reason"] == "invalid_direction", str(d))

# E-stop dominates everything
rs2.trigger_estop(reason="test2")
d = sl.check_and_apply("tester", MANUAL, 10, 10)
check("estop blocks commands", d["applied"] is False and d["reason"] == "estop", str(d))
rs2.reset_estop()

# After e-stop reset the robot is in STANDBY (never auto-resume motion).
check("mode is STANDBY after estop reset", rs2.get_mode() == STANDBY, rs2.get_mode())
rs2.request_mode(MANUAL, requested_by="tester")
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("command passes again after estop reset + re-engage", d["applied"] is True, str(d))
with sl._lock:
    sl._last_cmd_at["tester"] = time.time() - (core_config.MANUAL_CMD_TIMEOUT_S + 1)
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("stale command hits timeout", d["applied"] is False and d["reason"] == "timeout", str(d))

# force_stop always works
d = sl.force_stop("test")
check("force_stop clears state", d["reason"] == "stopped")


# ── 3. Motor service (dev mode, no lgpio) ────────────────────────────────────

print("\n[3] services.motor_service — differential math & compat API")
from services import motor_service as ms

# Point the safety singleton at a fresh, controlled setup
applied2: list[tuple[float, float]] = []
ms_sensors: dict = {}
from core.state import get_robot_state as grs
rs3 = grs()
rs3.reset_estop()
rs3.request_mode(MANUAL, requested_by="control_ws")

sl3 = get_safety_layer()
sl3.wire(motor_apply=lambda l, r: applied2.append((l, r)),
         sensor_provider=lambda: dict(ms_sensors))
ms_sensors.update({"front_distance_m": 2.0, "rear_distance_m": 2.0,
                   "left_cliff": False, "right_cliff": False})

ms.apply_locomotion(0.5, 0.0, "FORWARD")
check("FORWARD(0.5) reaches motors scaled by multiplier",
      applied2 and applied2[-1] == (50 * ms._state["speed_multiplier"],
                                    50 * ms._state["speed_multiplier"]),
      str(applied2[-1] if applied2 else None))

ms.apply_locomotion(0.0, 0.5, "LEFT")
left_duty, right_duty = applied2[-1]
check("LEFT rotates (left back, right forward)", left_duty < 0 < right_duty,
      str(applied2[-1]))

ms.apply_locomotion(0.0, 0.0, "STOP")
check("STOP zeros motors", applied2[-1] == (0.0, 0.0))

state = ms.get_state()
check("get_state() compat shape", {"estop_active", "mode", "speed_multiplier"} <= set(state))

estop_result = ms.trigger_estop()
check("trigger_estop via motor service", estop_result["estop_active"] is True)
applied2.clear()
ms.apply_locomotion(0.5, 0.0, "FORWARD")
# Safety gate may still write zero-duty to motors (a good thing); what must
# never happen is a non-zero command passing during e-stop.
check("no non-zero duty during estop",
      all(l == 0.0 and r == 0.0 for l, r in applied2), str(applied2))
reset_result = ms.reset_estop()
check("reset_estop via motor service", reset_result["estop_active"] is False)

mode_result = ms.set_mode(PATROL)
check("set_mode(PATROL) accepted", mode_result["active_mode"] == PATROL)
ms.stop_patrol_loop()

speed_result = ms.set_speed(1.5, 1.0)
check("set_speed clamps to ceiling", speed_result["speed_multiplier"] <= core_config.MAX_SPEED_MULTIPLIER)


# ── 4. App import integrity ──────────────────────────────────────────────────

print("\n[4] app import — full FastAPI app still assembles")
from main import app  # noqa: E402

# OpenAPI schema is the ground truth of what clients can call, and it
# resolves included routers on every FastAPI version.
schema_paths = set(app.openapi()["paths"].keys())

required = {
    "/api/v1/ping", "/api/v1/robot/mode", "/api/v1/robot/estop",
    "/api/v1/robot/estop/reset", "/api/v1/robot/speed",
    "/api/v1/telemetry/live", "/api/v1/camera/stream.mjpg",
}
check("all legacy REST routes still registered", required <= schema_paths,
      f"missing: {required - schema_paths}")


# ── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
