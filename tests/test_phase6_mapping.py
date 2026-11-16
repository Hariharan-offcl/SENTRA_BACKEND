"""
SENTRA — Phase 6 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase6_mapping.py

Covers: mapping session lifecycle (MANUAL auto-adopt, estop block), capture
via injected detections (dedup, context, frame save), naming persistence into
the tag map, capture deletion, TTL expiry, router handlers, app routes.
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


print("\n[1] setup — isolated tag map + fresh state")
import services.tag_map as tag_map

tmpdir = tempfile.mkdtemp()
tag_map.load(os.path.join(tmpdir, "tag_map.json"))
tag_map.delete_tag(1); tag_map.delete_tag(2); tag_map.delete_tag(3)
tag_map.delete_tag(4); tag_map.delete_tag(5)  # clean slate

import core.state as core_state
from core.state import RobotState, MANUAL, STANDBY, EMERGENCY_STOP
rs = RobotState()
core_state._robot_state = rs

from core.safety import SafetyLayer
import core.safety as core_safety
sl = SafetyLayer()
sl.wire(motor_apply=lambda l, r: None, sensor_provider=lambda: {})
sl.wire_event_reporter(lambda *a, **k: None)
core_safety._safety = sl

from services import mapping_service
mapping_service.SNAPSHOT_DIR = os.path.join(tmpdir, "maps")  # isolate snapshots

print("\n[2] session lifecycle")
r = mapping_service.start_session(started_by="test")
check("session starts", r["ok"] is True and r["session"]["active"] is True, str(r))
check("MANUAL auto-adopted for mapping", rs.get_mode() == MANUAL, rs.get_mode())

r2 = mapping_service.start_session()
check("double-start idempotent", r2["ok"] is True and r2.get("already_active") is True, str(r2))

check("stop works", mapping_service.stop_session()["ok"] is True)
check("session cleared", mapping_service.get_session() is None)
check("double-stop ok", mapping_service.stop_session().get("already_stopped") is True)

# E-stop blocks new sessions
rs.trigger_estop(reason="test")
r3 = mapping_service.start_session()
check("estop blocks session start", r3["ok"] is False and "estop" in r3["error"], str(r3))
rs.reset_estop()

print("\n[3] captures via injected detections")
mapping_service.start_session()

cap1 = mapping_service.capture_tag_detection(
    {"tag_id": 2, "timestamp": time.time(), "distance_m": 1.2, "bearing_deg": -4.0,
     "confidence": 0.9, "source": "phone"})
check("capture created", cap1 is not None and cap1["tag_id"] == 2, str(cap1))
check("unnamed initially", cap1["named"] is False and cap1["name"] is None)
check("context attached", "imu" in cap1["context"] and "wheel_encoders" in cap1["context"])

cap1b = mapping_service.capture_tag_detection(
    {"tag_id": 2, "timestamp": time.time(), "distance_m": 1.1, "bearing_deg": -3.0,
     "confidence": 0.9, "source": "phone"})
check("same tag deduped (seen_count)", cap1b["capture_id"] == cap1["capture_id"]
      and cap1b["seen_count"] == 2, str(cap1b))

cap2 = mapping_service.capture_tag_detection(
    {"tag_id": 3, "timestamp": time.time(), "distance_m": 0.8, "bearing_deg": 10.0,
     "confidence": 0.8, "source": "phone"})
check("second tag separate capture", cap2["capture_id"] != cap1["capture_id"])

caps = mapping_service.list_captures()
check("list shows 2 pending", len([c for c in caps if not c["named"]]) == 2, str(len(caps)))

print("\n[4] naming a capture persists into tag map")
r = mapping_service.name_capture(2, "Kitchen", "LOCATION")
check("naming ok", r["ok"] is True, str(r))
check("bound to capture", r["named_from_capture"] is True and r["capture_id"] == cap1["capture_id"], str(r))
check("tag map persisted", tag_map.get_tag(2)["name"] == "Kitchen")
check("capture marked named", mapping_service.get_capture(cap1["capture_id"])["named"] is True)

r = mapping_service.name_capture(9, "Office", "LOCATION")
check("direct naming without capture works", r["ok"] is True and r["named_from_capture"] is False)

r = mapping_service.name_capture(10, "", "LOCATION")
check("empty name rejected", r["ok"] is False, str(r))

print("\n[5] capture deletion")
check("discard unnamed capture", mapping_service.delete_capture(cap2["capture_id"]) is True)
check("named capture protected", mapping_service.delete_capture(cap1["capture_id"]) is False)
check("unknown capture False", mapping_service.delete_capture("nope") is False)

print("\n[6] TTL expiry of unnamed captures")
mapping_service.CAPTURE_TTL_S = 0.2
cap3 = mapping_service.capture_tag_detection(
    {"tag_id": 4, "timestamp": time.time(), "confidence": 0.9, "source": "phone"})
check("capture 4 pending", cap3 is not None)
time.sleep(0.4)
caps = mapping_service.list_captures()
check("expired unnamed capture gone", all(c["tag_id"] != 4 for c in caps), str(caps))
check("named capture survived TTL", any(c["tag_id"] == 2 for c in caps))
mapping_service.CAPTURE_TTL_S = 600.0
mapping_service.stop_session()

print("\n[7] on_frame subscriber path (apriltag correlation)")
mapping_service.start_session()
from services import apriltag_service
import numpy as np
import cv2

d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
img = cv2.aruco.generateImageMarker(d, 5, 220)
canvas = cv2.copyMakeBorder(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), 80, 80, 80, 80,
                            cv2.BORDER_CONSTANT, value=(30, 30, 30))
dets = apriltag_service.process_frame(canvas, frame_id="map-test-1")
check("detection produced for frame", len(dets) == 1 and dets[0]["tag_id"] == 5)
mapping_service.on_frame(canvas, "map-test-1")
caps = [c for c in mapping_service.list_captures() if not c["named"]]
check("on_frame captured tag 5", any(c["tag_id"] == 5 for c in caps), str([c["tag_id"] for c in caps]))
mapping_service.stop_session()

print("\n[8] router handlers")
from routers.mapping import start_mapping, stop_mapping, map_overview, name_map_tag, delete_map_tag
from models.mapping import MapTagRequest

resp = start_mapping()
check("POST /map/start handler", resp.ok is True)
name_map_tag(MapTagRequest(tag_id=6, name="Porch", type="LOCATION"))
check("POST /map/tag handler persisted", tag_map.get_tag(6)["name"] == "Porch")
overview = map_overview()
check("GET /map handler shape", {"active", "captures", "registered_tags", "unnamed_count"} <= set(overview.model_dump().keys()))
check("registered count includes new tags", overview.registered_tags >= 2)
resp = stop_mapping()
check("POST /map/stop handler", resp.ok is True)

print("\n[9] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/map/start", "/api/v1/map/stop", "/api/v1/map",
             "/api/v1/map/tag", "/api/v1/map/tag/{tag_id}", "/api/v1/map/status"}
check("all Phase 6 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("Phase 5 paths intact", "/api/v1/localization/status" in schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
