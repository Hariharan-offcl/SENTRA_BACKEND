"""
SENTRA — Phase 9 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase9_voice.py

Covers: target resolution (exact/prefix/substring/unknown), raw parsing
(wake word, bare location), dispatch (go_to → navigation engine SEEK/APPROACH/
ARRIVED, go_to_dock, start_patrol, stop, call_user ack), navigation engine
(search timeout, tag-lost re-seek), takeover, router handlers, app routes.
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


print("\n[1] setup — tag map + wired gate")
import services.tag_map as tag_map

tmpdir = tempfile.mkdtemp()
tag_map.load(os.path.join(tmpdir, "tag_map.json"))
for i in range(1, 6):
    tag_map.delete_tag(i)
tag_map.upsert_tag(1, "Dock", "DOCK")
tag_map.upsert_tag(2, "Kitchen", "LOCATION")
tag_map.upsert_tag(3, "Bedroom", "LOCATION")
tag_map.upsert_tag(4, "Hall", "LOCATION")

import core.state as core_state
from core.state import RobotState, NAVIGATION, MANUAL, STANDBY, PATROL
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

from services import voice_service, navigation_service, apriltag_service
from services import motor_service as ms
ms._h = None
ms.stop_patrol_loop()
navigation_service.start_engine()  # the go-to engine drives state transitions

print("\n[2] target resolution")
t = voice_service.resolve_target("KITCHEN")
check("case-insensitive exact", t is not None and t["tag_id"] == 2)
t = voice_service.resolve_target("  kitchen ")
check("whitespace tolerant", t is not None and t["tag_id"] == 2)
t = voice_service.resolve_target("kit")
check("prefix matches", t is not None and t["tag_id"] == 2)
t = voice_service.resolve_target("bed room")
check("multi-word substring", t is not None and t["tag_id"] == 3)
t = voice_service.resolve_target("atlantis")
check("unknown returns None", t is None)
t = voice_service.resolve_target("")
check("empty returns None", t is None)

print("\n[3] raw text parsing")
p = voice_service.parse_raw("SENTRA KITCHEN")
check("bare location after wake word", p == {"command": "go_to", "target": "kitchen"}, str(p))
p = voice_service.parse_raw("sentra go to bedroom")
check("go to pattern", p == {"command": "go_to", "target": "bedroom"}, str(p))
p = voice_service.parse_raw("ok sentra start patrol")
check("start patrol (wake word mid-utterance)", p == {"command": "start_patrol"}, str(p))
p = voice_service.parse_raw("sentra stop")
check("stop", p == {"command": "stop"}, str(p))
p = voice_service.parse_raw("sentra dock")
check("go to dock", p == {"command": "go_to_dock"}, str(p))
p = voice_service.parse_raw("sentra call family")
check("call user with target", p == {"command": "call_user", "target": "family"}, str(p))
p = voice_service.parse_raw("what time is it")
check("garbage → None", p is None, str(p))

print("\n[4] dispatch: go_to drives the navigation engine")
docking_cleanup = None
r = voice_service.execute("go_to", "kitchen")
check("handled", r.get("handled") is True, str(r))
check("mode NAVIGATION", rs.get_mode() == NAVIGATION, rs.get_mode())
check("session in SEEK or APPROACH", r.get("state") in ("SEEK", "APPROACH"), str(r))

# tag visible → approach; within arrival distance → arrive
apriltag_service.inject_detection(2, distance_m=0.40, bearing_deg=3.0)
time.sleep(0.5)
check("arrived → session cleared, STANDBY", navigation_service.status() is None
      and rs.get_mode() == STANDBY, str(rs.get_mode()))

r = voice_service.execute("go_to", "atlantis")
check("unknown location rejected with suggestions",
      r.get("handled") is False
      and any("kitchen" in k.lower() for k in r.get("known_locations", [])), str(r))

print("\n[5] navigation engine: tag lost → re-seek; search timeout")
r = voice_service.execute("go_to", "hall")
apriltag_service.inject_detection(4, distance_m=1.0, bearing_deg=0.0)
time.sleep(0.4)
check("in APPROACH", navigation_service.status()["state"] == "APPROACH")
# Detection ages out (grace 1.5s) → engine must drop back to SEEK.
# Poll for the transition instead of a fixed sleep — robust under load.
deadline = time.time() + 8.0
sess = navigation_service.status()
while (time.time() < deadline and sess is not None and sess["state"] != "SEEK"):
    time.sleep(0.1)
    sess = navigation_service.status()
check("tag lost → SEEK", sess is not None and sess["state"] == "SEEK", str(sess))

import core.config as core_config
saved = core_config.NAV_SEARCH_TIMEOUT_S
core_config.NAV_SEARCH_TIMEOUT_S = 0.8
voice_service.execute("go_to", "bedroom")  # ends previous via new go_to
time.sleep(4.2)  # old detection ages out + timeout fires
check("search timeout cancels", navigation_service.status() is None
      and rs.get_mode() == STANDBY, str(rs.get_mode()))
core_config.NAV_SEARCH_TIMEOUT_S = saved

print("\n[6] dispatch: go_to_dock, start_patrol, stop, call_user")
r = voice_service.execute("go_to_dock")
check("go_to_dock handled", r.get("handled") is True and r.get("target") == "Dock", str(r))
navigation_service.cancel("test")

r = voice_service.execute("start_patrol")
check("start_patrol handled (legacy fallback, no routes)", r.get("handled") is True, str(r))
check("PATROL mode entered", rs.get_mode() == PATROL)

r = voice_service.execute("stop")
check("stop handled → STANDBY", r.get("handled") is True and rs.get_mode() == STANDBY, str(r))

r = voice_service.execute("call_user", "family")
check("call_user acknowledged with Phase 13 note",
      r.get("handled") is True and "13" in (r.get("note") or ""), str(r))

r = voice_service.execute("dance")
check("unknown command rejected", r.get("handled") is False
      and "go_to" in r.get("commands", []), str(r))

r = voice_service.execute("", raw="sentra kitchen")
check("raw-only convenience works", r.get("handled") is True
      and r.get("action") == "go_to", str(r))
navigation_service.cancel("test")

print("\n[7] manual takeover cancels navigation")
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
mc.start()
voice_service.execute("go_to", "kitchen")
apriltag_service.inject_detection(2, distance_m=1.0, bearing_deg=0.0)
time.sleep(0.2)
mc.set_wheel_target(30, 30, source="takeover_ws")
time.sleep(0.6)
check("joystick → MANUAL", rs.get_mode() == MANUAL)
check("nav session cancelled", navigation_service.status() is None)
mc.stop("takeover_ws")
time.sleep(0.3)

print("\n[8] router handlers")
from routers.voice import voice_command, voice_commands
from models.voice import VoiceCommandRequest

resp = voice_command(VoiceCommandRequest(command="go_to", target="hall"))
check("POST /voice/command handler", resp.handled is True and resp.action == "go_to")
navigation_service.cancel("router_test")
resp = voice_command(VoiceCommandRequest(raw="sentra stop"))
check("raw form handler", resp.handled is True and resp.action == "stop")
resp = voice_commands()
check("GET /voice/commands handler", "go_to" in resp.commands and len(resp.examples) >= 5)

print("\n[9] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/voice/command", "/api/v1/voice/commands"}
check("all Phase 9 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/dock/return", "/api/v1/patrol/start"} <= schema_paths)

navigation_service.cancel("test_end")
navigation_service.stop_engine()

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
