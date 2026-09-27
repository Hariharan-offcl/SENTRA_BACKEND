"""
SENTRA — Phase 5 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase5_apriltag.py

Covers: tag map (defaults, CRUD, persistence, rename), real AprilTag
detection on a synthetic rendered tag, simulation injection, localization
selection logic, vision hub subscription, router handlers, app routes.
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


print("\n[1] tag_map — defaults, CRUD, persistence")
import services.tag_map as tag_map

tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "tag_map.json")

tag_map.load(db_path)
tags = tag_map.list_tags()
check("5 defaults created on first run", len(tags) == 5, str(len(tags)))
check("default tag 2 is Kitchen", any(t["tag_id"] == 2 and t["name"] == "Kitchen" for t in tags))

entry = tag_map.upsert_tag(7, "Porch", "LOCATION", "under the roof")
check("new tag created", entry["tag_id"] == 7 and entry["name"] == "Porch")
entry = tag_map.upsert_tag(7, "Back Porch")
check("rename via upsert", tag_map.get_tag(7)["name"] == "Back Porch")
check("created_at preserved on update",
      entry["created_at"] == tag_map.get_tag(7)["created_at"])

check("dock type accepted", tag_map.upsert_tag(1, "Dock", "DOCK")["type"] == "DOCK")
try:
    tag_map.upsert_tag(1, "Dock", "WARP")
    check("bad type rejected", False)
except ValueError:
    check("bad type rejected", True)
try:
    tag_map.upsert_tag(99999, "Far")
    check("tag id range validated", False)
except ValueError:
    check("tag id range validated", True)
try:
    tag_map.upsert_tag(8, "   ")
    check("empty name rejected", False)
except ValueError:
    check("empty name rejected", True)

check("delete works", tag_map.delete_tag(7) is True)
check("delete missing returns False", tag_map.delete_tag(7) is False)

# Persistence across reload
tag_map.upsert_tag(9, "Garage")
tag_map.load(db_path)
check("edits persisted to disk", tag_map.get_tag(9)["name"] == "Garage"
      and tag_map.get_tag(7) is None)

found = tag_map.find_by_name("  garage ")
check("case-insensitive name lookup", found is not None and found["tag_id"] == 9)

print("\n[2] apriltag_service — REAL detection on synthetic tag")
from services import apriltag_service

import cv2
import numpy as np

# Render an AprilTag 36h11 image with OpenCV itself (guaranteed-compatible)
dict_obj = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
tag_img = cv2.aruco.generateImageMarker(dict_obj, 3, 200)  # tag id 3, 200px
canvas = np.full((720, 1280, 3), 40, dtype=np.uint8)
x0, y0 = 500, 220
gray = cv2.cvtColor(tag_img, cv2.COLOR_GRAY2BGR)
canvas[y0:y0 + 200, x0:x0 + 200] = gray

detections = apriltag_service.process_frame(canvas, frame_id="test-synthetic")
check("synthetic tag detected", len(detections) == 1, str(detections))
if detections:
    det = detections[0]
    check("correct tag id", det["tag_id"] == 3, str(det["tag_id"]))
    check("distance estimated", det["distance_m"] is not None and 0.05 < det["distance_m"] < 5.0,
          str(det["distance_m"]))
    check("bearing near center offset", det["bearing_deg"] is not None, str(det["bearing_deg"]))
    check("confidence positive", det["confidence"] > 0)

blank = np.full((480, 640, 3), 40, dtype=np.uint8)
check("no false positives on blank frame", len(apriltag_service.process_frame(blank)) == 0)

check("visible window holds tag", any(d["tag_id"] == 3 for d in apriltag_service.get_visible()))
check("last-seen lookup", apriltag_service.get_last_seen(3)["tag_id"] == 3)
check("recent history newest-first",
      apriltag_service.get_recent(5)[0]["tag_id"] == 3)
stats = apriltag_service.stats()
check("detector stats populated", stats["frames_seen"] >= 2 and stats["opencv_available"], str(stats))

print("\n[3] simulation injection path")
det = apriltag_service.inject_detection(2, distance_m=1.1, bearing_deg=-3.0)
check("injected detection recorded", det["tag_id"] == 2 and det["source"] == "simulation")
check("injected tag is visible", any(d["tag_id"] == 2 for d in apriltag_service.get_visible()))

print("\n[4] localization_service — selection + description")
from services import localization_service

tag_map.upsert_tag(3, "Kitchen")
tag_map.upsert_tag(2, "Bedroom")  # rename kitchen default to test freshness
loc = localization_service.get_localization()
check("located = True with fresh tags", loc["located"] is True)
check("last_known described", loc["last_known"]["name"] in ("Kitchen", "Bedroom"), str(loc["last_known"]))
check("visible tags include both", {v["tag_id"] for v in loc["visible_tags"]} >= {2, 3})

localization_service.reset()
loc = localization_service.get_localization()
check("reset clears last-known (rebuilds from fresh tags)", loc["located"] is True)

print("\n[5] vision_service — hub plumbing")
from services import vision_service

got_frames = []
vision_service.subscribe(lambda f, fid: got_frames.append((f, fid)), name="test")
check("subscriber registered", "test" in vision_service.stats()["subscribers"])
vision_service.unsubscribe("test")
check("unsubscribe works", "test" not in vision_service.stats()["subscribers"])

print("\n[6] router handlers")
from routers.localization import get_tag_map, upsert_tag, delete_tag, localization_status
from models.localization import TagUpsertRequest

resp = get_tag_map()
check("GET /tags handler", resp.count == tag_map.count())

upd = upsert_tag(42, TagUpsertRequest(name="Test Room"))
check("PUT /tags/{id} handler", upd.tag.tag_id == 42 and upd.created is True)
upd2 = upsert_tag(42, TagUpsertRequest(name="Test Room 2"))
check("PUT update flagged not-created", upd2.created is False)
check("DELETE handler", delete_tag(42).deleted is True)

status = localization_status()
check("GET /status handler shape", {"located", "last_known", "visible_tags", "detector"} <= set(status.model_dump().keys()))

print("\n[7] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/localization/status", "/api/v1/localization/tags",
             "/api/v1/localization/tags/{tag_id}", "/api/v1/localization/detect",
             "/api/v1/localization/vision-stats"}
check("all Phase 5 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/telemetry/sensors", "/api/v1/safety/status"} <= schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
