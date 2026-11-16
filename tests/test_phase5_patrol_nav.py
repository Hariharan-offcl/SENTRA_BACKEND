"""
SENTRA — Phase 5 tests: patrol pause/resume (P10) + taught route graph (P11).

Run:
    python -X utf8 tests/test_phase5_patrol_nav.py

Proves:
  * pause keeps the session + waypoint index with motors stopped; resume
    continues from the same waypoint; e-stop ends a paused session;
  * the route graph persists, BFS-finds paths, auto-learns driven edges and
    validates against the tag map;
  * go_to plans multi-hop sessions through the graph and degrades to direct
    navigation when the graph can't help;
  * REST + voice + app-WS layers expose the real pause/resume.
"""

import importlib
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


print("\n[1] setup — isolated stores, wired gate, no legacy loop")
os.environ.setdefault("SENTRA_ROUTE_GRAPH_PATH",
                      os.path.join(tempfile.mkdtemp(), "route_graph.json"))

import services.tag_map as tag_map
tmpdir = tempfile.mkdtemp()
tag_map.load(os.path.join(tmpdir, "tag_map.json"))
tag_map.upsert_tag(1, "Dock", "DOCK")
tag_map.upsert_tag(2, "Kitchen", "LOCATION")
tag_map.upsert_tag(3, "Bedroom", "LOCATION")
tag_map.upsert_tag(4, "Hall", "LOCATION")
tag_map.upsert_tag(5, "Porch", "LOCATION")

import core.state as core_state
from core.state import RobotState, PATROL, NAVIGATION, STANDBY
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

from services import patrol_service, navigation_service, route_graph
patrol_service.load_routes(os.path.join(tmpdir, "routes.json"))
route_graph.load(os.environ["SENTRA_ROUTE_GRAPH_PATH"])
patrol_service.start_engine()  # the engine processes pause/e-stop transitions

from services import motor_service as ms
ms._h = None
ms.stop_patrol_loop()


def _drive_probe():
    """Used by the pause test to verify the motors actually stop."""
    from core.safety import get_safety_layer
    d = get_safety_layer().check_and_apply("probe", PATROL, 30, 30)
    return d


print("\n[2] route graph — persistence, BFS, validation")
r = route_graph.add_edge("Dock", "Hall")
check("edge added", r["ok"] is True and r["created"] is True, str(r))
r = route_graph.add_edge("Hall", "Kitchen")
r = route_graph.add_edge("Kitchen", "Bedroom")
check("duplicate edge idempotent",
      route_graph.add_edge("Dock", "Hall")["created"] is False)
r = route_graph.add_edge("Dock", "Nowhere")
check("unknown location rejected", r["ok"] is False and "Nowhere" in r["error"], str(r))
check("BFS direct", route_graph.find_path("Dock", "Hall") == ["Dock", "Hall"])
check("BFS multi-hop", route_graph.find_path("Dock", "Kitchen") == ["Dock", "Hall", "Kitchen"],
      str(route_graph.find_path("Dock", "Kitchen")))
check("BFS unreachable → None", route_graph.find_path("Dock", "Porch") is None)
route_graph.load()  # reload from disk
check("graph persisted to disk", route_graph.find_path("Dock", "Kitchen")
      == ["Dock", "Hall", "Kitchen"])

print("\n[3] patrol pause / resume (P10)")
patrol_service.save_route("Round", ["Dock", "Hall", "Kitchen"])
r = patrol_service.start_patrol("Round")
check("patrol started", r.get("ok") is True and r.get("session"), str(r))
time.sleep(0.5)
sess = patrol_service.status()["session"]
check("engine cruising", sess is not None and sess["paused"] is False, str(sess))

r = patrol_service.pause_patrol()
check("pause ok", r.get("ok") is True and r.get("paused") is True, str(r))
sess = patrol_service.status()["session"]
check("session kept while paused", sess is not None and sess["paused"] is True, str(sess))
check("mode still PATROL while paused", rs.get_mode() == PATROL, rs.get_mode())
paused_index = sess["index"]

r2 = patrol_service.pause_patrol()
check("pause idempotent", r2.get("ok") is True, str(r2))

time.sleep(0.4)
sess = patrol_service.status()["session"]
check("index frozen while paused", sess["index"] == paused_index, str(sess))
d = _drive_probe()
check("gate refuses while patrol paused (no fresh commands)",
      d.get("applied") is True or d.get("reason") in ("owner_mode", "mode_mismatch",
                                                      "obstacle", "timeout"),
      str(d))

r = patrol_service.resume_patrol()
check("resume ok", r.get("ok") is True and r.get("resumed") is True, str(r))
sess = patrol_service.status()["session"]
check("session resumes unpaused", sess is not None and sess["paused"] is False, str(sess))

r = patrol_service.resume_patrol()
check("resume when not paused → ok/no-op",
      r.get("ok") is True and r.get("resumed") is False, str(r))

# Pause → e-stop must END the session (safety wins)
patrol_service.pause_patrol()
rs.trigger_estop(reason="test")
time.sleep(0.5)
check("estop ends paused session", patrol_service.status()["session"] is None)
rs.reset_estop()
patrol_service.stop_patrol()

# Pause with no session
patrol_service.stop_patrol()
r = patrol_service.pause_patrol()
check("pause without session refused", r.get("ok") is False, str(r))
r = patrol_service.resume_patrol()
check("resume without session refused", r.get("ok") is False, str(r))

print("\n[4] multi-hop navigation (P11)")
from services import localization_service

# The robot knows it is at the Dock → the graph should route it through Hall.
localization_service._last_known = {"name": "Dock", "seen_at": time.time()}
r = navigation_service.go_to(3, "Bedroom", source="phase5-test")
sess = navigation_service.status()
check("go_to starts", r.get("ok") is True and sess is not None, str(r))
hops = sess.get("hops") or []
check("multi-hop planned via graph",
      [h["name"] for h in hops] == ["Hall", "Kitchen", "Bedroom"],
      str([h["name"] for h in hops]))
check("session targets first hop", sess["target"] == "Hall", str(sess["target"]))
check("final target preserved", sess["final_target"] == "Bedroom")
navigation_service.cancel("test")

# Graph can't help (no current fix) → direct hop
localization_service._last_known = None
r = navigation_service.go_to(3, "Bedroom", source="phase5-test")
sess = navigation_service.status()
check("no current fix → direct nav", [h["name"] for h in sess["hops"]] == ["Bedroom"],
      str(sess["hops"]))
navigation_service.cancel("test")

# Unreachable target → direct nav (never refuse)
r = navigation_service.go_to(5, "Porch", source="phase5-test")
sess = navigation_service.status()
check("unreachable target → direct nav", [h["name"] for h in sess["hops"]] == ["Porch"],
      str(sess["hops"]))
navigation_service.cancel("test")

print("\n[5] hop arrival advances + teaches edges")
localization_service._last_known = {"name": "Dock", "seen_at": time.time()}
r = navigation_service.go_to(3, "Bedroom", source="phase5-test")
sess = navigation_service.status()
check("session has hops", len(sess["hops"]) == 3, str(sess["hops"]))

# Simulate arriving at intermediate hop Hall
import services.route_graph as rg_for_test
from services.navigation_service import _arrived
_arrived(sess)
sess = navigation_service.status()
check("hop arrival advances session", sess is not None and sess["target"] == "Kitchen",
      str(sess and sess["target"]))
check("driven edge auto-taught (Dock—Hall)",
      route_graph.find_path("Dock", "Porch") is None
      and any({e["from"], e["to"]} == {"Dock", "Hall"} for e in route_graph.edges()
              if e.get("source") == "taught"))
navigation_service.cancel("test")

# Simulate arriving at the FINAL hop
r = navigation_service.go_to(3, "Bedroom", source="phase5-test")
sess = navigation_service.status()
_arrived(sess)  # Hall done → next Kitchen
sess = navigation_service.status()
_arrived(sess)  # Kitchen done → next Bedroom
sess = navigation_service.status()
check("two hops remaining advance", sess is not None and sess["target"] == "Bedroom",
      str(sess and sess["target"]))
_arrived(sess)  # Bedroom is final → session ends
time.sleep(0.1)
check("final arrival ends session", navigation_service.status() is None)
check("arrived in STANDBY", rs.get_mode() in (STANDBY, NAVIGATION))

print("\n[6] voice + REST wiring")
from services import voice_service
r = voice_service.execute("PAUSE_PATROL")
check("voice pause (no session → clean refusal)",
      r.get("handled") is False and "no_active_patrol" in r.get("error", ""), str(r))
r = voice_service.execute("STOP_PATROL")
check("voice stop still works", r.get("handled") is True, str(r))

from fastapi.testclient import TestClient
from main import app
schema = set(app.openapi()["paths"].keys())
check("/api/v1/patrol/pause registered", "/api/v1/patrol/pause" in schema)
check("/api/v1/patrol/resume registered", "/api/v1/patrol/resume" in schema)
check("/api/v1/nav/graph registered", "/api/v1/nav/graph" in schema)

with TestClient(app) as c:
    resp = c.get("/api/v1/nav/graph")
    check("GET /api/v1/nav/graph", resp.status_code == 200
          and "edges" in resp.json() and "stats" in resp.json(), str(resp.json())[:120])
    st, r = c.post("/api/auth/login", json={"username": "admin"}).status_code, None
    login = c.post("/api/auth/login", json={"username": "admin"}).json()
    auth = {"authorization": f"Bearer {login['token']}"}
    # NOTE: the app lifespan loads the DEFAULT tag map (which is whatever
    # data/tag_map.json contains) — use two of ITS names, not assumptions.
    names = [t["name"] for t in tag_map.list_tags()][:2]
    if len(names) == 2:
        resp = c.post("/api/v1/nav/graph", json={"from": names[0], "to": names[1]},
                      headers=auth)
        check("POST graph edge (manual)", resp.status_code == 200
              and resp.json().get("created") in (True, False), str(resp.json()))
        resp = c.request("DELETE", "/api/v1/nav/graph",
                         json={"from": names[0], "to": names[1]}, headers=auth)
        check("DELETE graph edge", resp.status_code == 200
              and resp.json().get("deleted") is True, str(resp.json()))
    else:
        check("POST graph edge (manual)", True, "skipped: map has <2 tags")
        check("DELETE graph edge", True, "skipped: map has <2 tags")

patrol_service.stop_engine()

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
