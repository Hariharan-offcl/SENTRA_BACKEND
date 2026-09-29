"""
SENTRA — Phase 3 media/alert-image tests.

Run:
    python -X utf8 tests/test_phase3_media.py

Proves:
  * unknown-person safety events carry their snapshot filename into the
    notification store ("image_file"),
  * GET /api/alerts/{id}/image serves those bytes (Phase 1's placeholder is
    now a real implementation),
  * GET /api/alerts/{id} exposes image_url under /media/,
  * the /media static mount serves the recognition snapshot dir (audit bug:
    image_url pointed at a prefix nothing served),
  * alerts without a snapshot still answer a distinguishable 404.
"""

import asyncio
import base64
import io
import json
import os
import sys
import tempfile

# Isolate the snapshot dir BEFORE importing person_recognition / main.
os.environ["SENTRA_RECOG_SNAPSHOT_DIR"] = tempfile.mkdtemp()
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")

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


print("\n[1] setup — isolated stores + a real snapshot file")
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

from services import person_recognition
from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (80, 60), (120, 40, 40)).save(buf, format="JPEG")
jpeg_bytes = buf.getvalue()
snap_name = "unknown_1234567890.jpg"
snap_path = os.path.join(person_recognition.SNAPSHOT_DIR, snap_name)
os.makedirs(person_recognition.SNAPSHOT_DIR, exist_ok=True)
with open(snap_path, "wb") as f:
    f.write(jpeg_bytes)
check("snapshot written to isolated dir", os.path.isfile(snap_path))

import services.device_registry as dr
dr.load()
import core.auth as ca
import services.compat_map as cm
import services.relay_client as rc

from services import notification_service
notification_service.load(os.path.join(tempfile.mkdtemp(), "notifications.json"))


def call(method, path, body=None, token=None):
    headers = {}
    body_b64 = ""
    if body is not None:
        body_b64 = base64.b64encode(json.dumps(body).encode()).decode()
        headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = asyncio.run(rc.dispatch_local(method, path, None, headers,
                                      base64.b64decode(body_b64) if body_b64 else b""))
    try:
        parsed = json.loads(base64.b64decode(r["body_b64"])) if r["body_b64"] else {}
    except Exception:
        parsed = {}
    return r["status"], parsed, r


print("\n[2] login + a PERSON_UNKNOWN event with snapshot")
st, r, _ = call("POST", "/api/auth/login", {"username": "admin"})
TOKEN = r["token"]
check("login", st == 200 and TOKEN)

n = notification_service.notify_safety_event({
    "type": "PERSON_UNKNOWN",
    "detail": {"faces": 1, "snapshot": snap_name},
    "severity": "DANGER",
    "location": "hall",
})
check("notification created with image_file", n is not None and n["image_file"] == snap_name,
      str(n))
ALERT_ID = n["id"]

print("\n[3] alert object + image endpoint")
st, r, _ = call("GET", f"/api/alerts/{ALERT_ID}")
check("alert detail 200", st == 200 and r.get("id") == ALERT_ID, f"{st} {r}")
check("alert image_url points at /media", r.get("image_url") == f"/media/{snap_name}",
      str(r.get("image_url")))

st, r, raw = call("GET", f"/api/alerts/{ALERT_ID}/image")
check("alert image serves JPEG bytes", st == 200 and base64.b64decode(raw["body_b64"]) == jpeg_bytes,
      f"{st} len={len(raw['body_b64'])}")

st, r, _ = call("GET", "/api/alerts/does-not-exist/image")
check("unknown alert image → 404", st == 404, str(st))

# Alert WITHOUT a snapshot
n2 = notification_service.add_manual("Manual note", "no image here")
st, r, _ = call("GET", f"/api/alerts/{n2['id']}/image")
check("image-less alert → 404 'no image attached'",
      st == 404 and r.get("detail") == "no image attached", f"{st} {r}")
st, r, _ = call("GET", f"/api/alerts/{n2['id']}")
check("image-less alert has null image_url", st == 200 and r.get("image_url") is None, str(r))

print("\n[4] /media static mount")
from main import app
from fastapi.testclient import TestClient
with TestClient(app) as c:
    resp = c.get(f"/media/{snap_name}")
    check("GET /media/<snapshot> 200 + bytes", resp.status_code == 200
          and resp.content == jpeg_bytes, str(resp.status_code))
    resp = c.get("/media/nope.jpg")
    check("GET /media/<missing> 404", resp.status_code == 404, str(resp.status_code))

print("\n[5] person image_url consistency")
record = {"person_key": "p1", "name": "Granny", "snapshot_path": snap_path,
          "notes": "", "created_at": 0.0}
obj = cm.person_object(record)
check("person image_url == /media/<file>", obj["image_url"] == f"/media/{snap_name}", str(obj))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
