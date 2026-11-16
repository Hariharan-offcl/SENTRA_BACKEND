"""
SENTRA — Phase 13 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase13_emergency_call.py

Covers: fall confirmation → RINGING session, evidence refresh (no duplicate
invite), answer → ACTIVE, peers-gone → ENDED, cooldown, ack, ring timeout →
MISSED, disabled switch, fall-reset silences ringing, role semantics,
router handlers, app routes.
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


print("\n[1] setup + env tuning")
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

import importlib
import services.fall_detection as fd
importlib.reload(fd)
import services.emergency_call as ec
importlib.reload(ec)
ec.register_with_fall()   # attach fresh hook to fresh fall_detection

check("defaults: ring 30s", ec.RING_TIMEOUT_S == 30.0, str(ec.RING_TIMEOUT_S))
check("defaults: cooldown 60s", ec.COOLDOWN_S == 60.0, str(ec.COOLDOWN_S))

os.environ["SENTRA_EMERGENCY_RING_S"] = "1.0"
os.environ["SENTRA_EMERGENCY_COOLDOWN_S"] = "2.0"
importlib.reload(ec)
check("env override: ring 1s", ec.RING_TIMEOUT_S == 1.0, str(ec.RING_TIMEOUT_S))
check("env override: cooldown 2s", ec.COOLDOWN_S == 2.0, str(ec.COOLDOWN_S))
del os.environ["SENTRA_EMERGENCY_RING_S"]
del os.environ["SENTRA_EMERGENCY_COOLDOWN_S"]
importlib.reload(ec)
ec.register_with_fall()

CONFIRM = fd.CONFIRM_TIME_S
T0 = time.time()


def confirm_fall(person_id):
    """Drive one person through a confirmed fall (synthetic timestamps)."""
    fd.sample(person_id, [300, 60, 120, 360], 0.9, timestamp=T0)
    fd.sample(person_id, [180, 280, 360, 140], 0.9, timestamp=T0 + 1.0)
    return fd.sample(person_id, [180, 280, 360, 140], 0.9,
                     timestamp=T0 + 1.0 + CONFIRM + 0.1)


def clear_emergency():
    with ec._lock:
        ec._session = None
        ec._history.clear()


print("\n[2] fall confirmation → RINGING session")
fd.reset()
r = confirm_fall(2)
check("person confirmed", r["state"] == "FALL_CONFIRMED", str(r))
st = ec.get_status()
check("session exists", st["session"] is not None, str(st))
check("state RINGING", st["session"]["state"] == "RINGING", str(st["session"]))
check("reason FALL", st["session"]["reason"] == "FALL")
check("evidence carries person 2", st["session"]["evidence"]["person_id"] == 2)
check("session id EMG-*", st["session"]["session_id"].startswith("EMG-"))
payload = ec._payload("invite", st["session"])
check("invite payload type", payload["type"] == "emergency_call"
      and payload["action"] == "invite")
check("invite points at existing call WS", payload["ws"]["signaling"] == "/ws/webrtc/user"
      and payload["ws"]["video"] == "/ws/call/user")

print("\n[3] second confirmation refreshes evidence, no duplicate session")
r = confirm_fall(3)
st = ec.get_status()
check("still one RINGING session", st["session"]["state"] == "RINGING")
check("evidence refreshed to person 3", st["session"]["evidence"]["person_id"] == 3,
      str(st["session"]["evidence"]))

print("\n[4] caregiver answers (user signaling socket) → ACTIVE")
ec.note_presence("user", True)
st = ec.get_status()
check("state ACTIVE", st["session"]["state"] == "ACTIVE", str(st["session"]))
check("user_connected", st["session"]["user_connected"] is True)

print("\n[5] role semantics: node alone never answers")
clear_emergency()
confirm_fall(4)
ec.note_presence("node", True)
st = ec.get_status()
check("node connect keeps RINGING", st["session"]["state"] == "RINGING",
      str(st["session"]))
check("node_connected tracked", st["session"]["node_connected"] is True)

print("\n[6] peers leave → ENDED + history")
ec.note_presence("user", True)   # answer
ec.note_presence("user", False)  # leave (node still marked but not connected-as-peer? node was connected)
ec.note_presence("node", False)
st = ec.get_status()
check("session cleared after peers gone", st["session"] is None, str(st))
check("last_session ENDED", st["last_session"]["state"] == "ENDED",
      str(st["last_session"]))

print("\n[7] cooldown suppresses duplicate sessions")
before = st["last_session"]["session_id"]
r = confirm_fall(5)
st = ec.get_status()
check("no new session during cooldown", st["session"] is None, str(st["session"]))
check("last session unchanged", st["last_session"]["session_id"] == before,
      str(st["last_session"]))
clear_emergency()

print("\n[8] ack ends a ringing session")
confirm_fall(6)
resp = ec.ack("on my way")
check("ack returns session", resp is not None and resp["acknowledged"] is True,
      str(resp))
check("ack note stored", resp["note"] == "on my way")
st = ec.get_status()
check("ringing session ended by ack", st["session"] is None
      and st["last_session"]["state"] == "ENDED", str(st))
check("ack with no session → None", ec.ack() is None)
clear_emergency()

print("\n[9] ring timeout → MISSED (lazy deadline, no timer thread)")
ec.RING_TIMEOUT_S = 0.05  # module constant read at call time
confirm_fall(7)
deadline = time.time() + 1.0
state = None
while time.time() < deadline:
    state = (ec.get_status()["session"] or {}).get("state")
    if ec.get_status()["last_session"]:
        break
    time.sleep(0.02)
st = ec.get_status()
check("ringing expired → MISSED", st["session"] is None
      and st["last_session"]["state"] == "MISSED", str(st))
ec.RING_TIMEOUT_S = 30.0
clear_emergency()

print("\n[10] disabled switch blocks sessions")
ec.ENABLED = False
confirm_fall(8)
check("no session when disabled", ec.get_status()["session"] is None)
ec.ENABLED = True
clear_emergency()

print("\n[11] fall reset silences a ringing emergency")
confirm_fall(9)
check("ringing before reset", ec.get_status()["session"]["state"] == "RINGING")
fd.reset()
st = ec.get_status()
check("fall reset ended the ringing call", st["session"] is None
      and st["last_session"]["state"] == "ENDED", str(st))
check("marked acknowledged", st["last_session"]["acknowledged"] is True)
clear_emergency()

print("\n[12] router handlers")
from routers.emergency import emergency_status, emergency_ack, emergency_history
from models.emergency import EmergencyAckRequest

status = emergency_status()
check("GET /emergency/status handler", status.enabled is True
      and status.tuning.ring_timeout_s == 30.0, str(status))
confirm_fall(10)
resp = emergency_ack(EmergencyAckRequest(note="handling it"))
check("POST /emergency/ack handler", resp.ok is True
      and resp.session.acknowledged is True, str(resp))
hist = emergency_history()
check("GET /emergency/history handler", len(hist.sessions) >= 1
      and hist.sessions[-1].state == "ENDED", str(hist.sessions[-1] if hist.sessions else None))
clear_emergency()

print("\n[13] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/emergency/status", "/api/v1/emergency/ack",
             "/api/v1/emergency/history"}
check("all Phase 13 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/fall/status", "/api/v1/persons/recognize"}
      <= schema_paths)
check("path count grew past pre-Phase-13 baseline",
      len(schema_paths) >= 73, str(len(schema_paths)))  # newest suite owns exact count

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
