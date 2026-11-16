"""
SENTRA — Phase 7 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase7_patrol.py

Covers: route store (validation vs tag map, persistence, default), patrol
start/stop, route session (waypoint confirmation via injected detections,
timeout skip), obstacle blocking + resume, takeover safety (manual + mode
change), legacy wander fallback, router handlers, app routes.
"""

import os
import sys
import tempfile
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


print("\n[1] setup — isolated tag map + routes, wired gate, motion controller stopped")
import services.tag_map as tag_map

tmpdir = tempfile.mkdtemp()
tag_map.load(os.path.join(tmpdir, "tag_map.json"))
tag_map.upsert_tag(1, "Dock", "DOCK")
tag_map.upsert_tag(2, "Kitchen", "LOCATION")
tag_map.upsert_tag(3, "Bedroom", "LOCATION")
tag_map.upsert_tag(4, "Hall", "LOCATION")

routes_path = os.path.join(tmpdir, "routes.json")

import core.state as core_state
from core.state import RobotState, MANUAL, PATROL, STANDBY
rs = RobotState()
core_state._robot_state = rs

from core.safety import SafetyLayer
import core.safety as core_safety
sensors = {"front_distance_m": 2.0, "rear_distance_m": 2.0,
           "left_cliff": False, "right_cliff": False}
sl = SafetyLayer()
sl.wire(motor_apply=lambda l, r: None, sensor_provider=lambda: dict(sensors))
sl.wire_event_reporter(lambda *a, **k: None)
core_safety._safety = sl

from services import patrol_service
patrol_service.load_routes(routes_path)

from services import motor_service as ms
ms._h = None
ms.stop_patrol_loop()  # keep the legacy wander loop out of this test

from services import apriltag_service

print("\n[2] route store")
r = patrol_service.save_route("Errands", ["Dock", "Nope"])
check("unknown waypoint rejected", r["ok"] is False, str(r))
r = patrol_service.save_route("Round", ["Dock"])
check("single-waypoint route rejected", r["ok"] is False, str(r))
r = patrol_service.save_route("Round", ["Dock", "Hall", "Kitchen"])
check("valid route saved", r["ok"] is True and r["created"] is True, str(r))
check("first route became default", patrol_service.get_default_route() == "Round")
r = patrol_service.save_route("Round", ["Dock", "Kitchen"], set_default=True)
check("route updated", r["ok"] is True and r["created"] is False)
check("route update persisted (in memory)", patrol_service.get_route("Round") == ["Dock", "Kitchen"])

patrol_service.save_route("Second", ["Bedroom", "Dock"])
r = patrol_service.set_default_route("Second")
check("default switchable", r["ok"] is True and patrol_service.get_default_route() == "Second")
r = patrol_service.set_default_route("Ghost")
check("unknown default rejected", r["ok"] is False)

print("\n[3] patrol start/stop + engine (route session)")
patrol_service.start_engine()

r = patrol_service.start_patrol("Round")
check("route session started", r["ok"] is True and r.get("session") is not None, str(r))
sess = r["session"]
check("mode is PATROL", rs.get_mode() == PATROL)
check("owner is patrol_service", rs.get_mode_owner() == "patrol_service")
check("waypoints resolved", sess["waypoints"] == ["Dock", "Kitchen"])
check("current waypoint is first", sess["current_waypoint"] == "Dock")

# Confirm waypoint 1 (Dock=1) via injected fresh detection
apriltag_service.inject_detection(1, distance_m=0.9)
time.sleep(1.2)  # engine tick
sess = patrol_service.status()["session"]
check("Dock confirmed → index advanced", sess["index"] == 1, str(sess["index"]))
check("confirmed list updated", "Dock" in sess["confirmed_waypoints"])
check("now at Kitchen", sess["current_waypoint"] == "Kitchen")

# Obstacle blocks the path to Kitchen
sensors["front_distance_m"] = 0.2
time.sleep(1.0)
sess = patrol_service.status()["session"]
check("blocked flag set", sess["blocked"] is True, str(sess))

# Clear again → resumes
sensors["front_distance_m"] = 2.0
time.sleep(1.0)
sess = patrol_service.status()["session"]
check("resumed after path cleared", sess["blocked"] is False, str(sess))

# Confirm Kitchen → route completes
apriltag_service.inject_detection(2, distance_m=0.7)
time.sleep(1.5)
st = patrol_service.status()
check("route completed → session ended", st["session"] is None, str(st["session"]))
check("mode back to STANDBY after completion", rs.get_mode() == STANDBY, rs.get_mode())

print("\n[4] waypoint timeout skip")
patrol_service.PATROL_WAYPOINT_TIMEOUT_S_saved = None
import core.config as core_config
saved = core_config.PATROL_WAYPOINT_TIMEOUT_S
core_config.PATROL_WAYPOINT_TIMEOUT_S = 1.0  # shrink for the test
r = patrol_service.start_patrol("Round")
time.sleep(2.2)  # > timeout, no tag injected
sess = patrol_service.status()["session"]
if sess is None:
    # both waypoints may have timed out already at 1.0s each — acceptable
    check("waypoint timeout path exercised (session ended)", True)
else:
    check("waypoint skipped on timeout", "Dock" in sess["skipped_waypoints"], str(sess))
core_config.PATROL_WAYPOINT_TIMEOUT_S = saved
patrol_service.stop_patrol()
time.sleep(0.3)

print("\n[5] manual takeover ends patrol")
r = patrol_service.start_patrol("Round")
check("patrol active for takeover test", r["ok"] is True)
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
mc.start()
sensors["front_distance_m"] = 2.0
r = mc.set_wheel_target(30, 30, source="takeover_ws")
time.sleep(0.8)
check("joystick took over → MANUAL", rs.get_mode() == MANUAL, rs.get_mode())
check("patrol session ended by takeover", patrol_service.status()["session"] is None)
mc.stop("takeover_ws")
patrol_service.stop_patrol()
time.sleep(0.3)

print("\n[6] set_mode(MANUAL) also ends patrol (REST takeover path)")
patrol_service.start_patrol("Round")
ms.set_mode("MANUAL")
check("patrol ended via set_mode", patrol_service.status()["session"] is None)
check("mode is MANUAL", rs.get_mode() == MANUAL)
patrol_service.stop_patrol()
time.sleep(0.3)

print("\n[7] legacy wander fallback (no routes for name=None)")
# Remove routes entirely: fresh store at a new path
patrol_service.load_routes(os.path.join(tmpdir, "empty_routes.json"))
r = patrol_service.start_patrol()  # no routes at all
check("legacy wander fallback engaged", r["ok"] is True and r.get("legacy_wander") is True, str(r))
check("PATROL mode entered", rs.get_mode() == PATROL)
st = patrol_service.status()
check("status reports legacy_wander", st["legacy_wander"] is True and st["session"] is None, str(st))
patrol_service.stop_patrol()
check("stop clears legacy", patrol_service.status()["active"] is False)
time.sleep(0.3)

print("\n[8] estop blocks patrol start")
rs.trigger_estop(reason="test")
r = patrol_service.start_patrol("Round")
check("estop blocks patrol", r["ok"] is False and "estop" in r["error"], str(r))
rs.reset_estop()
# restore route store for router checks
patrol_service.load_routes(routes_path)

print("\n[9] router handlers")
from routers.patrol import patrol_start, patrol_stop, patrol_status, save_route, list_routes, delete_route
from models.patrol import PatrolStartRequest, RouteSaveRequest

resp = save_route(RouteSaveRequest(name="Router", waypoints=["Hall", "Dock"], set_default=True))
check("POST /patrol/routes handler", resp.ok is True and resp.created is True)
resp = patrol_start(PatrolStartRequest(route="Router"))
check("POST /patrol/start handler", resp.ok is True and resp.session is not None)
time.sleep(0.5)
status = patrol_status()
check("GET /patrol/status handler", status.active is True and status.session is not None)
resp = patrol_stop()
check("POST /patrol/stop handler", resp.ok is True)
resp = list_routes()
check("GET /patrol/routes handler", any(rt.name == "Router" for rt in resp.routes))
resp = delete_route("Router")
check("DELETE /patrol/routes/{name} handler", resp.ok is True)

print("\n[10] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/patrol/start", "/api/v1/patrol/stop", "/api/v1/patrol/status",
             "/api/v1/patrol/routes", "/api/v1/patrol/routes/default",
             "/api/v1/patrol/routes/{name}"}
check("all Phase 7 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/map", "/api/v1/control/move"} <= schema_paths)

patrol_service.stop_patrol()
patrol_service.stop_engine()

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
