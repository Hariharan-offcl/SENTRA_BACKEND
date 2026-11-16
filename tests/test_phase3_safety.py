"""
SENTRA — Phase 3 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase3_safety.py

Covers: runtime safety config (bounds, atomic batches, reset), cliff service,
safety events (debounce, counters), dynamic gate behavior (threshold changes
take effect immediately), REST router, and app route registration.
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


print("\n[1] core.safety_config — runtime thresholds")
from core.safety_config import SafetyConfig, get_safety_config

# Use the SINGLETON — the safety gate and REST layer read this same object.
cfg = get_safety_config()
base = cfg.snapshot()

r = cfg.update({"front_obstacle_stop_m": 0.75})
check("valid update applied", r["updated"] == ["front_obstacle_stop_m"], str(r))
check("value live", cfg.get("front_obstacle_stop_m") == 0.75)

r = cfg.update({"front_obstacle_stop_m": 0.01})
check("below bound rejected", "front_obstacle_stop_m" in r["rejected"], str(r))
check("rejected value unchanged", cfg.get("front_obstacle_stop_m") == 0.75)

r = cfg.update({"front_obstacle_stop_m": 99.0})
check("above bound rejected", "front_obstacle_stop_m" in r["rejected"])

r = cfg.update({"front_obstacle_stop_m": 0.5, "bogus_key": 1.0})
check("atomic batch: valid key rejected alongside bad key",
      r["updated"] == [] and "bogus_key" in r["rejected"], str(r))

r = cfg.update({"cliff_stop": False})
check("cliff_stop bool toggle", cfg.get_bool("cliff_stop") is False)
cfg.update({"cliff_stop": True})

r = cfg.update({"max_speed_multiplier": 1.5})
check("speed ceiling bounded at 1.0", "max_speed_multiplier" in r["rejected"])

cfg.update({"front_obstacle_stop_m": base["front_obstacle_stop_m"]})
check("limits() reports bounds", cfg.limits()["front_obstacle_stop_m"] == [0.10, 2.00])

check("singleton exists", get_safety_config() is not None)

print("\n[2] services.cliff_service — simulated + provider fragment")
from services import cliff_service

cliff_service.start_monitoring()  # simulated on dev machine (no lgpio)
st = cliff_service.get_state()
check("simulated on dev machine", st["simulated"] is True, str(st))
check("no cliff in simulation", st["left_cliff"] is False and st["right_cliff"] is False)
frag = cliff_service.sensor_provider()
check("provider fragment shape", set(frag.keys()) == {"left_cliff", "right_cliff"})

print("\n[3] services.safety_events — debounce + counters")
from services import safety_events

e1 = safety_events.report("OBSTACLE", {"sensor": "front", "distance_m": 0.2})
check("first event recorded", e1 is not None)
e2 = safety_events.report("OBSTACLE", {"sensor": "front"})
check("immediate repeat debounced", e2 is None)
time.sleep(2.1)
e3 = safety_events.report("OBSTACLE", {"sensor": "front"})
check("repeat after debounce window recorded", e3 is not None)
e4 = safety_events.report("BOGUS", {})
check("unknown type rejected", e4 is None)
check("counters incremented", safety_events.get_counters()["OBSTACLE"] == 2)
check("history capped and ordered", len(safety_events.get_history(10)) <= 10)
safety_events.report("CLIFF", {"side": "left"}, severity="DANGER")
check("stats shape", "counters" in safety_events.stats())

print("\n[4] gate uses RUNTIME thresholds (dynamic behavior)")
import core.state as core_state
from core.state import RobotState, MANUAL
from core.safety import SafetyLayer

rs = RobotState()
core_state._robot_state = rs
rs.request_mode(MANUAL, requested_by="tester")

applied = []
sensors = {"front_distance_m": 0.6, "rear_distance_m": 2.0, "left_cliff": False, "right_cliff": False}
reported = []
sl = SafetyLayer()
sl.wire(motor_apply=lambda l, r: applied.append((l, r)),
        sensor_provider=lambda: dict(sensors))
sl.wire_event_reporter(lambda t, d, s: reported.append((t, d, s)))
import core.safety as core_safety
core_safety._safety = sl

# With default 0.40 m stop, 0.6 m is clear → passes
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("0.6m clear at default 0.40m threshold", d["applied"] is True, str(d))

# Tighten threshold to 0.8 m → same distance now blocks
cfg.update({"front_obstacle_stop_m": 0.8})
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("same distance blocked after threshold tightened", d["applied"] is False, str(d))
check("obstacle event reported", any(t == "OBSTACLE" for t, _, _ in reported))

# Loosen back → passes again
cfg.update({"front_obstacle_stop_m": 0.4})
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("clear again after threshold restored", d["applied"] is True)

# Runtime speed ceiling affects clamping
cfg.update({"max_speed_multiplier": 0.5})
d = sl.check_and_apply("tester", MANUAL, 500, 500)
check("runtime ceiling clamps to 50", d["applied"] and d["left"] == 50.0, str(d))
cfg.update({"max_speed_multiplier": 1.0})

# Cliff toggle via runtime config
sensors["left_cliff"] = True
cfg.update({"cliff_stop": False})
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("cliff ignored when disabled", d["applied"] is True, str(d))
cfg.update({"cliff_stop": True})
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("cliff enforced when enabled", d["applied"] is False)
sensors["left_cliff"] = False

# Timeout uses runtime value
cfg.update({"manual_cmd_timeout_s": 0.3})
with sl._lock:
    sl._last_cmd_at["tester"] = time.time() - 0.5
d = sl.check_and_apply("tester", MANUAL, 50, 50)
check("runtime timeout triggers", d["applied"] is False and d["reason"] == "timeout", str(d))
cfg.update({"manual_cmd_timeout_s": 0.6})

print("\n[5] status() snapshot")
status = sl.status()
check("status keys", {"mode", "estop_active", "watchdog_active", "last_decision",
                      "sensors", "thresholds"} <= set(status.keys()), str(status.keys()))
check("status shows live sensor", status["sensors"]["front_distance_m"] == 0.6)

print("\n[6] REST router /api/v1/safety/*")
from routers.safety import safety_status, get_thresholds, update_thresholds, reset_thresholds
from models.safety import ThresholdUpdateRequest

resp = get_thresholds()
check("GET thresholds", resp.front_obstacle_stop_m == 0.4, str(resp))

upd = update_thresholds(ThresholdUpdateRequest(front_obstacle_stop_m=0.9))
check("PUT thresholds applied", upd.updated == ["front_obstacle_stop_m"], str(upd))
check("PUT response echoes new value", upd.thresholds.front_obstacle_stop_m == 0.9)

# 0.07 passes the Pydantic field bound (ge=0.05) but fails the stricter
# SafetyConfig bound (lo=0.10) — proving the second validation layer works.
upd = update_thresholds(ThresholdUpdateRequest(front_obstacle_stop_m=0.07))
check("PUT out-of-bounds rejected", upd.rejected.get("front_obstacle_stop_m", "").startswith("out_of_bounds"), str(upd))

status_resp = safety_status()
check("GET status wired", status_resp.thresholds.front_obstacle_stop_m == 0.9)
reset_thresholds()
check("POST reset restores defaults", get_thresholds().front_obstacle_stop_m == base["front_obstacle_stop_m"])

print("\n[7] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/safety/status", "/api/v1/safety/events",
             "/api/v1/safety/thresholds", "/api/v1/safety/thresholds/limits",
             "/api/v1/safety/thresholds/reset"}
check("all Phase 3 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", "/api/v1/control/move" in schema_paths and "/api/v1/robot/estop" in schema_paths)

print("\n[8] regressions: motion controller uses runtime rates")
from services.motion_controller import MotionController, _step_toward
import core.config as core_config
mc2 = MotionController()  # not started; only math check
cfg.update({"accel_pct_per_s": 300.0})
check("accel rate now 300 → step 15 @50ms",
      _step_toward(0.0, 50.0, 0.05) == 15.0, str(_step_toward(0.0, 50.0, 0.05)))
cfg.update({"accel_pct_per_s": core_config.ACCEL_PCT_PER_S})

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
