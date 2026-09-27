"""
SENTRA — Phase 14 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase14_notifications.py

Covers: manual notifications, safety-event mirroring via listener (incl.
debounce behavior), Flutter /alerts contract shape + ack, FALL end-to-end
mirroring, ack-all/unread/stats, persistence across reload, router handlers,
app routes (legacy + new).
"""

import os
import re
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


print("\n[1] setup")
import services.tag_map as tag_map
tag_map.load(os.path.join(tempfile.mkdtemp(), "tag_map.json"))
import core.state as core_state
from core.state import RobotState
core_state._robot_state = RobotState()
from core.safety import SafetyLayer
import core.safety as core_safety
core_safety._safety = SafetyLayer()
core_safety._safety.wire(motor_apply=lambda l, r: None, sensor_provider=lambda: {})
core_safety._safety.wire_event_reporter(lambda *a, **k: None)

_npath = os.path.join(tempfile.mkdtemp(), "notifications.json")
os.environ["SENTRA_NOTIFICATIONS_PATH"] = _npath

import importlib
import services.safety_events as safety_events
importlib.reload(safety_events)          # fresh listeners + debounce clocks
import services.notification_service as ns
importlib.reload(ns)                     # picks up the temp persistence path
import services.fall_detection as fd
importlib.reload(fd)                     # binds to the reloaded safety_events

safety_events.add_listener(ns.notify_safety_event)
check("listener registered", len(safety_events._listeners) == 1)
check("persistence path overridden", ns.PATH == _npath, ns.PATH)

TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2} • .+$")

print("\n[2] manual notification")
n = ns.add_manual("Test title", "Test description", "warning")
check("id prefix NOTIF-", n["id"].startswith("NOTIF-"), n["id"])
check("severity normalized to WARNING", n["severity"] == "WARNING", n["severity"])
check("flutter timestamp format", TS_RE.match(n["timestamp"]) is not None,
      n["timestamp"])
check("event_type MANUAL", n["event_type"] == "MANUAL")
check("unread is 1", ns.unread_count() == 1, str(ns.unread_count()))
items = ns.get()
check("newest first", items[0]["id"] == n["id"], str(items[0]["id"]))

print("\n[3] safety-event mirroring via listener")
e = safety_events.report("CLIFF", {"side": "left"}, severity="DANGER")
check("event reported", e is not None)
mirrored = [x for x in ns.get() if x["event_type"] == "CLIFF"]
check("CLIFF mirrored", len(mirrored) == 1, str(len(mirrored)))
check("danger title prefixed", mirrored[0]["title"].startswith("Emergency:"),
      mirrored[0]["title"])
check("danger severity kept", mirrored[0]["severity"] == "DANGER")

safety_events.report("PERSON", {"persons": 2, "person_ids": [1, 2]},
                     severity="INFO")
person = [x for x in ns.get() if x["event_type"] == "PERSON"]
check("PERSON mirrored as INFO", len(person) == 1
      and person[0]["severity"] == "INFO", str(person))
check("person description counts", "2 person(s) in view" in person[0]["description"],
      person[0]["description"])

safety_events.report("OBSTACLE", {"sensor": "front", "distance_m": 0.2})
safety_events.report("OBSTACLE", {"sensor": "front"})  # debounced → dropped
obs = [x for x in ns.get() if x["event_type"] == "OBSTACLE"]
check("debounced duplicate not mirrored", len(obs) == 1, str(len(obs)))

print("\n[4] Flutter /alerts contract (legacy endpoints, real data)")
from routers.alerts import get_alerts, acknowledge_alert
from models.responses import AlertItem

resp = get_alerts(severity="ALL", limit=100)
check("returns AlertsListResponse", hasattr(resp, "alerts") and len(resp.alerts) >= 3)
first = resp.alerts[0]
check("AlertItem has exactly the 6 contract fields",
      set(first.model_dump().keys()) ==
      {"id", "title", "timestamp", "description", "severity", "acknowledged"},
      str(set(first.model_dump().keys())))
check("mock ALT-101 gone", all(not a.id.startswith("ALT-") for a in resp.alerts))

filtered = get_alerts(severity="DANGER", limit=100)
check("severity filter works", all(a.severity == "DANGER" for a in filtered.alerts)
      and len(filtered.alerts) >= 1)

target = first.id
ack = acknowledge_alert(target)
check("ack via legacy route", ack.acknowledged is True and ack.alert_id == target)
ack2 = acknowledge_alert("NOTIF-does-not-exist")
check("ack unknown id → acknowledged False", ack2.acknowledged is False)

print("\n[5] FALL end-to-end through fall_detection")
CONFIRM = fd.CONFIRM_TIME_S
t0 = time.time()
fd.sample(42, [300, 60, 120, 360], 0.9, timestamp=t0)
fd.sample(42, [180, 280, 360, 140], 0.9, timestamp=t0 + 1.0)
fd.sample(42, [180, 280, 360, 140], 0.9, timestamp=t0 + 1.0 + CONFIRM + 0.1)
fall = [x for x in ns.get() if x["event_type"] == "FALL"]
check("FALL notification created", len(fall) == 1, str(len(fall)))
check("FALL is DANGER + emergency title", fall[0]["severity"] == "DANGER"
      and fall[0]["title"].startswith("Emergency:"), fall[0]["title"])
check("FALL description names person 42", "person 42" in fall[0]["description"],
      fall[0]["description"])

print("\n[6] ack-all, unread, stats")
unread_before = ns.unread_count()
res = ns.ack_all()
check("ack_all counts everything unacknowledged", res == unread_before,
      f"{res} vs {unread_before}")
check("unread now 0", ns.unread_count() == 0)
st = ns.stats()
check("stats shape", st["enabled"] is True and st["total"] >= 5
      and st["unread"] == 0 and st["by_severity"].get("DANGER", 0) >= 2, str(st))

print("\n[7] persistence across reload")
before = {x["id"]: x["acknowledged"] for x in ns.get(limit=500)}
importlib.reload(ns)   # fresh module state — simulates process restart
ns.load()              # startup path (as main.py lifespan does)
after = {x["id"]: x["acknowledged"] for x in ns.get(limit=500)}
check("notifications survive reload", len(after) >= 5, str(len(after)))
check("acknowledged flags persisted", after == before)
check("file at configured path", os.path.exists(_npath))
safety_events.add_listener(ns.notify_safety_event)  # re-attach after reload

print("\n[8] notification router handlers")
from routers.notifications import list_notifications, ack_all as r_ack_all, \
    notification_stats, push_test
from models.notifications import NotificationTestRequest

lst = list_notifications(severity="ALL", limit=100)
check("GET /notifications handler", len(lst.notifications) >= 5
      and lst.unread == 0)
t = push_test(NotificationTestRequest(title="Router test", description="d",
                                      severity="WARNING"))
check("POST /notifications/test handler", t.event_type == "MANUAL"
      and t.acknowledged is False and t.severity == "WARNING")
lst2 = list_notifications(severity="WARNING", limit=100)
check("router severity filter", all(x.severity == "WARNING"
      for x in lst2.notifications) and len(lst2.notifications) >= 1)
acked = r_ack_all()
check("POST /notifications/ack-all handler", acked.ok is True
      and acked.acknowledged >= 1)
s = notification_stats()
check("GET /notifications/stats handler", s.total >= 6 and s.unread == 0)

print("\n[9] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/notifications", "/api/v1/notifications/ack-all",
             "/api/v1/notifications/stats", "/api/v1/notifications/test"}
check("all Phase 14 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("legacy alerts contract intact",
      {"/api/v1/alerts", "/api/v1/alerts/{alert_id}/ack"} <= schema_paths)
check("earlier phases intact", {"/api/v1/emergency/status", "/api/v1/fall/status"}
      <= schema_paths)
check("path count grew past pre-Phase-14 baseline",
      len(schema_paths) >= 77, str(len(schema_paths)))  # newest suite owns exact count

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
