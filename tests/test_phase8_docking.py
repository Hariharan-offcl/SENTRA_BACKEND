"""
SENTRA — Phase 8 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase8_docking.py

Covers: dock tag resolution, SEEK (rotation → found / timeout), APPROACH
(bearing steering, slow-near, stop distance, tag-lost re-seek), ALIGN
(tolerance), takeover + e-stop cancellation, router handlers, app routes.
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


print("\n[1] setup — isolated tag map with DOCK, wired gate")
import services.tag_map as tag_map

tmpdir = tempfile.mkdtemp()
tag_map.load(os.path.join(tmpdir, "tag_map.json"))
for i in range(1, 6):
    tag_map.delete_tag(i)
tag_map.upsert_tag(1, "Dock", "DOCK")
tag_map.upsert_tag(2, "Kitchen", "LOCATION")

import core.state as core_state
from core.state import RobotState, RETURN_TO_DOCK, MANUAL, STANDBY
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

from services import docking_service, apriltag_service
from services import motor_service as ms
ms._h = None
ms.stop_patrol_loop()

print("\n[2] dock tag resolution")
check("dock tag resolves to 1", docking_service.dock_tag_id() == 1)
tag_map.delete_tag(1)
check("no dock tag → None", docking_service.dock_tag_id() is None)
r = docking_service.start_return()
check("start without dock tag rejected", r["ok"] is False and r["error"] == "no_dock_tag_registered", str(r))
tag_map.upsert_tag(1, "Dock", "DOCK")

print("\n[3] SEEK: finds tag → APPROACH")
docking_service.start_engine()
r = docking_service.start_return()
check("session started", r["ok"] is True and r["session"]["state"] == "SEEK", str(r))
check("mode RETURN_TO_DOCK", rs.get_mode() == RETURN_TO_DOCK)
check("owner docking_service", rs.get_mode_owner() == "docking_service")

apriltag_service.inject_detection(1, distance_m=1.2, bearing_deg=5.0)
time.sleep(0.4)
sess = docking_service.status()
check("SEEK → APPROACH on tag sighted", sess["state"] == "APPROACH", str(sess))

print("\n[4] APPROACH: steered drive, stop distance")
time.sleep(0.5)  # let the engine cruise toward the tag
sess = docking_service.status()
check("still approaching (1.2m > 0.30m stop)", sess["state"] == "APPROACH", str(sess))

# Move within stop distance but with bearing OUTSIDE tolerance → ALIGN holds
apriltag_service.inject_detection(1, distance_m=0.22, bearing_deg=30.0)
time.sleep(0.4)
sess = docking_service.status()
check("reached dock distance → ALIGN", sess is not None and sess["state"] == "ALIGN", str(sess))

print("\n[5] ALIGN: rotates until bearing within tolerance → DOCKED")
# Keep the tag visible with a large bearing for one tick (rotation continues)
apriltag_service.inject_detection(1, distance_m=0.22, bearing_deg=25.0)
time.sleep(0.2)
sess = docking_service.status()
check("still aligning with 25° bearing", sess is not None and sess["state"] == "ALIGN", str(sess))
# Now bring bearing within tolerance → docks
apriltag_service.inject_detection(1, distance_m=0.22, bearing_deg=2.0)
time.sleep(0.5)  # engine tick: bearing 2° < 8° tol → docked
check("session ended docked", docking_service.status() is None)
check("mode STANDBY after docking", rs.get_mode() == STANDBY, rs.get_mode())

print("\n[6] APPROACH: tag lost → back to SEEK")
docking_service.start_return()
apriltag_service.inject_detection(1, distance_m=1.0, bearing_deg=0.0)
time.sleep(0.4)
check("in APPROACH", docking_service.status()["state"] == "APPROACH")
# Let the detection age out beyond the grace window (1.5s) without re-injecting.
# Wait on the actual transition instead of a fixed sleep — robust under load.
deadline = time.time() + 8.0
sess = docking_service.status()
while (time.time() < deadline and sess is not None and sess["state"] != "SEEK"):
    time.sleep(0.1)
    sess = docking_service.status()
check("tag lost → SEEK again", sess is not None and sess["state"] == "SEEK", str(sess))

print("\n[7] manual takeover cancels docking")
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
mc.start()
apriltag_service.inject_detection(1, distance_m=1.0, bearing_deg=0.0)  # keep it moving
r = mc.set_wheel_target(30, 30, source="takeover_ws")
time.sleep(0.6)
check("mode flipped to MANUAL by joystick", rs.get_mode() == MANUAL, rs.get_mode())
check("docking session cancelled", docking_service.status() is None)
mc.stop("takeover_ws")
time.sleep(0.3)

print("\n[8] SEEK timeout aborts")
# Purge injected detections so the tag is NOT fresh — deterministic test.
with apriltag_service._lock:
    apriltag_service._last_by_tag.pop(1, None)
import core.config as core_config
saved = core_config.DOCK_SEARCH_TIMEOUT_S
core_config.DOCK_SEARCH_TIMEOUT_S = 0.8
docking_service.start_return()
time.sleep(1.4)  # > 0.8s timeout + engine margin
check("search timeout ended session", docking_service.status() is None)
check("mode STANDBY after timeout", rs.get_mode() == STANDBY)
core_config.DOCK_SEARCH_TIMEOUT_S = saved

print("\n[9] e-stop cancels docking")
docking_service.start_return()
apriltag_service.inject_detection(1, distance_m=1.0, bearing_deg=0.0)
time.sleep(0.2)
rs.trigger_estop(reason="test")
time.sleep(0.4)
check("estop ended docking", docking_service.status() is None)
check("mode EMERGENCY_STOP", rs.get_mode() == "EMERGENCY_STOP")
rs.reset_estop()
time.sleep(0.2)

print("\n[10] router handlers")
from routers.docking import dock_return, dock_cancel, dock_status
from models.docking import DockReturnRequest

resp = dock_return(DockReturnRequest())
check("POST /dock/return handler", resp.ok is True and resp.session.state in ("SEEK", "APPROACH"))
status = dock_status()
check("GET /dock/status handler", status.active is True and status.dock_tag_id == 1)
resp = dock_cancel()
check("POST /dock/cancel handler", resp.ok is True and resp.was_active is True)
resp = dock_cancel()
check("cancel when idle", resp.ok is True and resp.was_active is False)

print("\n[11] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/dock/return", "/api/v1/dock/cancel", "/api/v1/dock/status"}
check("all Phase 8 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/patrol/start", "/api/v1/map"} <= schema_paths)

docking_service.cancel_return("test_end")
docking_service.stop_engine()

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
