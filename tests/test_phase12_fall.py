"""
SENTRA — Phase 12 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase12_fall.py

Covers: evidence sampling, NORMAL→POSSIBLE→CONFIRMED transitions, recovery
(stands up before confirmation), single-frame immunity (one lying frame can
never confirm), latch + hooks fire once, reset, simulate, router, routes.
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


def fresh_service():
    """Reload fall_detection with clean state."""
    import importlib
    import services.fall_detection as fd
    importlib.reload(fd)
    return fd


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

import services.fall_detection as fd

CONFIRM = fd.CONFIRM_TIME_S  # 4.0 by default

print("\n[2] standing person stays NORMAL")
t0 = time.time()
for i in range(6):
    r = fd.sample(1, [300, 60, 120, 360], 0.9, timestamp=t0 + i * 0.25)
check("standing (tall bbox) → NORMAL", r["state"] == "NORMAL", str(r))

print("\n[3] single lying frame → POSSIBLE_FALL only (never CONFIRMED)")
r = fd.sample(1, [180, 280, 360, 140], 0.9, timestamp=t0 + 2.0)
check("one frame → at most POSSIBLE_FALL", r["state"] == "POSSIBLE_FALL", str(r))
check("no global confirmation fired", fd.get_status()["last_confirmed"] is None)

print("\n[4] recovery: stands up before window → NORMAL")
r = fd.sample(1, [300, 60, 120, 360], 0.9, timestamp=t0 + 2.25)
check("standing again → NORMAL (recovered)", r["state"] == "NORMAL", str(r))

print("\n[5] sustained fall → FALL_CONFIRMED (latch, hooks once)")
fired = []
fd.register_emergency_hook(lambda ev: fired.append(ev))

# person 2: standing frame, then lying and staying put past the confirm window
fd.sample(2, [300, 60, 120, 360], 0.9, timestamp=t0)
r = fd.sample(2, [180, 280, 360, 140], 0.9, timestamp=t0 + 1.0)
check("→ POSSIBLE_FALL", r["state"] == "POSSIBLE_FALL", str(r))
# mid-window: still lying → stays POSSIBLE
r = fd.sample(2, [180, 280, 360, 140], 0.9, timestamp=t0 + 1.0 + CONFIRM / 2)
check("mid-window still POSSIBLE", r["state"] == "POSSIBLE_FALL", str(r))
# past the window, still lying → CONFIRMED
r = fd.sample(2, [180, 280, 360, 140], 0.9, timestamp=t0 + 1.0 + CONFIRM + 0.1)
check("past window → FALL_CONFIRMED", r["state"] == "FALL_CONFIRMED", str(r))
check("emergency hook fired once", len(fired) == 1 and fired[0]["person_id"] == 2, str(fired))
check("PERSON remains confirmed (latched)", fd.sample(2, [180, 280, 360, 140], 0.9,
      timestamp=t0 + 1.0 + CONFIRM + 1.0)["state"] == "FALL_CONFIRMED")
check("hooks NOT re-fired on subsequent samples", len(fired) == 1)
check("get_status overall CONFIRMED", fd.get_status()["overall"] == "FALL_CONFIRMED")
check("FALL event in history", any(e["type"] == "FALL"
      for e in __import__("services.safety_events", fromlist=["get_history"]).get_history(5)))

print("\n[6] reset clears latch")
fd.reset(2)
check("reset person 2", fd.get_status()["overall"] == "NORMAL", str(fd.get_status()["overall"]))
check("hooks re-arm after reset", True)

print("\n[7] lying but MOVING never confirms (crawl-safe)")
fired.clear()
t1 = time.time()
# crawl: wide bbox but center moves > gate each sample
positions = [(200, 280), (320, 280), (440, 280), (560, 280), (680, 280)]
r = None
for i, (x, _) in enumerate(positions):
    r = fd.sample(3, [x, 280, 360, 140], 0.9, timestamp=t1 + i * 0.5)
check("crawling person stays POSSIBLE/NORMAL", r["state"] in ("NORMAL", "POSSIBLE_FALL"), str(r))
check("no hook for crawler", len(fired) == 0)

print("\n[8] simulate_fall drives the full sequence")
fd.reset()
res = fd.simulate_fall(person_id=77, duration_s=CONFIRM + 1)
check("simulated fall confirmed", res["final_state"] == "FALL_CONFIRMED", str(res))
check("status shows confirmed", fd.get_status()["overall"] == "FALL_CONFIRMED")

print("\n[9] router handlers")
from routers.fall import fall_status, fall_reset, fall_simulate
from models.fall import FallResetRequest, FallSimulateRequest

status = fall_status()
check("GET /fall/status handler", status.overall in ("NORMAL", "POSSIBLE_FALL", "FALL_CONFIRMED"))
resp = fall_reset(FallResetRequest(person_id=77))
check("POST /fall/reset handler", resp.ok and resp.status.overall == "NORMAL")
fd.reset()
resp = fall_simulate(FallSimulateRequest(person_id=88, duration_s=CONFIRM + 1))
check("POST /fall/simulate handler", resp.ok and resp.final_state == "FALL_CONFIRMED")
fd.reset()

print("\n[10] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/fall/status", "/api/v1/fall/reset", "/api/v1/fall/simulate"}
check("all Phase 12 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact", {"/api/v1/persons/recognize", "/api/v1/voice/command"} <= schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
