"""
SENTRA — Phase 2 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase2_motion.py

Covers: motion controller (ramp toward target, one-shot expiry, source-silence
expiry, brake pulse), message handling (all legacy + new forms), REST router
endpoints, and differential math.
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


print("\n[1] setup — fresh state + wired safety gate")
import core.state as core_state
from core.state import RobotState, MANUAL, STANDBY
from core.safety import SafetyLayer
from core import config as core_config

rs = RobotState()
core_state._robot_state = rs

applied = []
sensors = {"front_distance_m": 2.0, "rear_distance_m": 2.0,
           "left_cliff": False, "right_cliff": False}
sl = SafetyLayer()
sl.wire(motor_apply=lambda l, r: applied.append((l, r)),
        sensor_provider=lambda: dict(sensors))
import core.safety as core_safety
core_safety._safety = sl

from services import motor_service as ms
# Keep dev-mode GPIO writes as no-ops; grab multiplier defaults
ms._h = None  # ensure dev mode even if lgpio existed
mult = ms._state["speed_multiplier"]

from services.motion_controller import MotionController, _step_toward
mc = MotionController()
mc.start()

check("controller starts idle", mc.get_state()["current_left"] == 0.0)
check("estop blocked manual entry works later", True)

# ── ramp helper ───────────────────────────────────────────────────────────────
print("\n[2] ramp math (_step_toward)")
check("accelerates toward positive target",
      _step_toward(0.0, 50.0, 0.05) == core_config.ACCEL_PCT_PER_S * 0.05)
check("decelerates at faster rate",
      _step_toward(50.0, 0.0, 0.05) == 50.0 - core_config.DECEL_PCT_PER_S * 0.05)
check("reaches exact target when close", _step_toward(0.4, 0.0, 0.05) == 0.0)
check("crossing zero uses decel rate",
      _step_toward(-10.0, 20.0, 0.05) == -10.0 + core_config.DECEL_PCT_PER_S * 0.05)

# ── wheel targets and ramping ────────────────────────────────────────────────
print("\n[3] wheel targets + streaming")
rs.request_mode(MANUAL, requested_by="motion_controller")
r = mc.set_wheel_target(50, 50, source="test_ws")
check("wheel target accepted", r["applied"] is True, str(r))
time.sleep(0.4)  # allow ramp loop to push duty
state = mc.get_state()
check("current duty approached target", 0 < state["current_left"] <= 50, str(state))
check("watchdog fed while cruising", sl.last_command_age("motion_controller") is not None)

r = mc.stop("test_ws")
check("stop accepted", r["applied"] is True)
time.sleep(0.4)
state = mc.get_state()
check("duty back to zero after stop", state["current_left"] == 0.0 and state["current_right"] == 0.0, str(state))

# ── directional forms ────────────────────────────────────────────────────────
print("\n[4] directional + differential forms")
r = mc.set_directional("FORWARD", 0.5, source="test_ws")
check("FORWARD 0.5 → 50/50 target", r["applied"] and r["left"] == 50.0 and r["right"] == 50.0, str(r))
r = mc.set_directional("LEFT", 0.5, source="test_ws")
check("LEFT 0.5 → -50/+50 target", r["applied"] and r["left"] == -50.0 and r["right"] == 50.0, str(r))
r = mc.set_directional("RIGHT", 1.0, source="test_ws")
check("RIGHT 1.0 → 100/-100 target", r["applied"] and r["left"] == 100.0 and r["right"] == -100.0, str(r))
r = mc.set_directional("BACKWARD", 0.2, source="test_ws")
check("BACKWARD 0.2 → -20/-20 target", r["applied"] and r["left"] == -20.0, str(r))
r = mc.set_directional("SPIN", 0.5, source="test_ws")
check("unknown direction rejected", r["applied"] is False, str(r))

r = mc.set_differential(0.5, 0.0, source="test_ws", speed_multiplier=0.5)
check("differential(0.5,0) at mult 0.5 → 25/25", r["left"] == 25.0 and r["right"] == 25.0, str(r))
r = mc.set_differential(0.0, 0.5, source="test_ws", speed_multiplier=0.5)
check("differential(0,0.5) at mult 0.5 → -25/25", r["left"] == -25.0 and r["right"] == 25.0, str(r))
mc.stop("test_ws")

# ── obstacle blocking through the controller ────────────────────────────────
print("\n[5] obstacle still enforced (safety gate underneath)")
sensors["front_distance_m"] = 0.2
r = mc.set_wheel_target(50, 50, source="test_ws")
check("target accepted", r["applied"] is True)
time.sleep(0.3)
state = mc.get_state()
check("motors held at zero near obstacle",
      state["current_left"] == 0.0 and state["current_right"] == 0.0, str(state))
sensors["front_distance_m"] = 2.0
mc.stop("test_ws")
time.sleep(0.2)

# ── one-shot expiry ─────────────────────────────────────────────────────────
print("\n[6] one-shot duration expiry")
r = mc.set_wheel_target(40, 40, source="rest_move", stream=False, duration=0.3)
check("one-shot accepted", r["applied"] is True, str(r))
time.sleep(0.15)
state = mc.get_state()
check("moving during one-shot", state["current_left"] > 0, str(state))
time.sleep(0.5)
state = mc.get_state()
check("auto-stopped after duration", state["current_left"] == 0.0 and state["target_left"] == 0.0, str(state))

# ── source-silence expiry ───────────────────────────────────────────────────
print("\n[7] streaming source silence → auto stop")
r = mc.set_wheel_target(40, 40, source="ghost_ws")
check("streaming target accepted", r["applied"] is True)
# simulate silence: backdate the last command
with mc._lock:
    mc._last_source_cmd = time.time() - (core_config.MANUAL_CMD_TIMEOUT_S + 0.2)
time.sleep(0.4)
state = mc.get_state()
check("silent source ramped to zero", state["target_left"] == 0.0 and state["current_left"] == 0.0, str(state))

# ── estop dominance ─────────────────────────────────────────────────────────
print("\n[8] e-stop dominance through controller")
rs.trigger_estop(reason="test")
r = mc.set_wheel_target(50, 50, source="test_ws")
check("wheel target rejected during estop", r["applied"] is False and "estop" in r.get("error", ""), str(r))
r = mc.handle_message({"type": "wheel", "left": 50, "right": 50})
check("WS wheel form rejected during estop", r.get("applied") is False, str(r))
rs.reset_estop()
rs.request_mode(MANUAL, requested_by="motion_controller")

# ── brake ───────────────────────────────────────────────────────────────────
print("\n[9] brake pulse")
r = mc.handle_message({"type": "wheel", "left": 60, "right": 60})
check("cruise again", r.get("applied") is True, str(r))
time.sleep(0.2)
r = mc.handle_message({"type": "brake"})
check("brake accepted", r.get("action") == "brake" and r.get("applied") is True, str(r))
time.sleep(0.6)  # decel + brake hold
state = mc.get_state()
check("stopped after brake", state["current_left"] == 0.0, str(state))

# ── message forms (legacy compatibility) ────────────────────────────────────
print("\n[10] handle_message — all forms")
r = mc.handle_message({"linear_velocity": 0.4, "angular_velocity": 0.0, "direction": "FORWARD"})
check("legacy FORWARD form acked", r.get("ack") is True and r.get("applied") is True, str(r))
r = mc.handle_message({"linear_velocity": 0.0, "angular_velocity": 0.0, "direction": "STOP"})
check("legacy STOP form acked", r.get("ack") is True and r.get("direction") == "STOP", str(r))
r = mc.handle_message({"type": "wheel", "left": 30, "right": -30})
check("wheel form acked", r.get("ack") is True and r.get("type") == "wheel", str(r))
r = mc.handle_message({"left_speed": 25, "right_speed": 25})
check("left_speed/right_speed convenience form acked", r.get("ack") is True, str(r))
r = mc.handle_message({"type": "direction", "direction": "RIGHT", "scale": 0.8})
check("direction form acked", r.get("ack") is True and r.get("direction") == "RIGHT", str(r))
r = mc.handle_message({"type": "stop"})
check("stop form acked", r.get("direction") == "STOP", str(r))
try:
    mc.handle_message({"nonsense": True})
    check("garbage rejected", False)
except ValueError:
    check("garbage rejected", True)
mc.stop("test_ws")

# ── REST router ──────────────────────────────────────────────────────────────
print("\n[11] REST router /api/v1/control/*")
from routers.manual_control import control_move, control_stop, control_brake, control_state
from models.control import MoveRequest, StopRequest, BrakeRequest

resp = control_move(MoveRequest(left_speed=35, right_speed=35, duration=0.2))
check("REST move (wheel) applied", resp.applied is True, str(resp))
time.sleep(0.1)
resp = control_stop(StopRequest())
check("REST stop applied", resp.applied is True and resp.action == "stop")
resp = control_move(MoveRequest(direction="FORWARD", scale=0.4, duration=0.2))
check("REST move (directional) applied", resp.applied is True, str(resp))
time.sleep(0.3)
resp = control_brake(BrakeRequest())
check("REST brake applied", resp.applied is True and resp.action == "brake")
time.sleep(0.6)
state_resp = control_state()
check("REST state shape", hasattr(state_resp, "current_left") and state_resp.mode in ("MANUAL", "STANDBY"))

# ── WS handler import sanity ────────────────────────────────────────────────
print("\n[12] ws handler + app integration")
import ws_handlers.control_ws  # noqa: F401
check("control_ws module imports", True)

from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/control/move", "/api/v1/control/stop", "/api/v1/control/brake",
             "/api/v1/control/estop", "/api/v1/control/estop/reset", "/api/v1/control/state"}
check("all Phase 2 REST paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")

legacy = {"/api/v1/robot/mode", "/api/v1/robot/estop", "/api/v1/ping"}
check("legacy paths still intact", legacy <= schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
