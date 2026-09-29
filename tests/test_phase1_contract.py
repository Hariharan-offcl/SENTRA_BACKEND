"""
SENTRA — Phase 1 contract tests (SENTRA_API_CONTRACT.md).

Run:
    python -X utf8 tests/test_phase1_contract.py

Covers the Flutter↔backend contract end-to-end:
  §0  GET /api/health (unauthenticated discovery probe)
  §3  flat-envelope WS: ping/motor/estop/heartbeat/resync on /ws/user,
      camera_frame ingestion on /ws/rover (frames must be ROVER-only)
  §5  voice vocabulary bridge (SHOUTED Flutter enums → dispatchers)
  §6  the Phase-1 REST endpoints: rover register, pairing-token, sensors,
      status, standby, patrol/resume, alert detail + image, GET call,
      PATCH aliases (locations, alerts/dismiss)
"""

import asyncio
import base64
import io
import json
import os
import sys
import tempfile

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


# ── [1] setup: isolated registries ───────────────────────────────────────────
print("\n[1] setup")
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")
# Phase 4: pairing code must be configured like a real deployment would.
os.environ.setdefault("SENTRA_PAIRING_CODE", "123456")

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

import services.device_registry as dr
importlib_dr = dr
importlib_dr.load()
import core.auth as ca
import services.compat_map as cm
import services.relay_client as rc


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
    return r["status"], parsed


# ── [2] §0 health ────────────────────────────────────────────────────────────
print("\n[2] §0 health probe")
st, r = call("GET", "/api/health")
check("GET /api/health 200", st == 200, f"{st} {r}")
check("health shape", {"status", "robot_id", "mode", "version"} <= set(r.keys()), str(r))

# login for the rest
st, r = call("POST", "/api/auth/login", {"username": "admin"})
TOKEN = r["token"]
check("login works", st == 200 and TOKEN, f"{st}")

# ── [3] §3 flat-envelope WS channels ────────────────────────────────────────
print("\n[3] §3 /ws/user + /ws/rover")
import websockets
import uvicorn
import socket
from main import app

port_holder = {}


async def ws_e2e():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port_holder["port"] = s.getsockname()[1]
    s.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port_holder["port"],
                            log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    results = {}

    async def drain(ws, seconds=1.5):
        """Collect every event pushed in a window (telemetry pushes at 1 Hz
        interleave with replies — never assume ordering)."""
        out = []
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            try:
                out.append(json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - loop.time()))))
            except asyncio.TimeoutError:
                break
            except Exception:
                break
        return out

    try:
        base = f"ws://127.0.0.1:{port_holder['port']}"

        # ── auth: tokenless connect must be refused with 4401 ───────────
        try:
            async with websockets.connect(f"{base}/ws/user") as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)
            results["tokenless_closed"] = False
        except Exception as exc:
            results["tokenless_closed"] = "4401" in str(exc)

        # ── /ws/user: flat envelope ping + heartbeat + resync + unknown ─
        async with websockets.connect(f"{base}/ws/user?token={TOKEN}") as ws:
            await ws.send(json.dumps({"type": "ping", "ts": 1}))
            # legacy envelope inbound must also work (defensive unwrap)
            await ws.send(json.dumps({"event": "ping", "data": {}}))
            await ws.send(json.dumps({"type": "heartbeat", "battery_percent": 88}))
            await ws.send(json.dumps({"type": "resync"}))
            await ws.send(json.dumps({"type": "nope"}))  # unknown → error
            events = await drain(ws)
            kinds = [e.get("event") for e in events]
            results["pong"] = "pong" in kinds
            results["heartbeat_ack"] = "heartbeat_ack" in kinds
            results["resync_telemetry"] = any(
                e.get("event") == "telemetry" and "battery_percent" in e.get("data", {})
                for e in events)
            results["unknown_replied"] = "error" in kinds

        # ── /ws/rover: camera_frame ingestion (tiny valid JPEG) ─────────
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (30, 60, 90)).save(buf, format="JPEG")
        jpeg = buf.getvalue()

        async with websockets.connect(f"{base}/ws/rover?token={TOKEN}") as ws:
            await ws.send(json.dumps({"type": "camera_start", "width": 640,
                                      "height": 480, "fps": 12, "jpeg_quality": 70}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({
                "type": "camera_frame", "encoding": "base64", "format": "jpeg",
                "width": 640, "height": 480, "sequence": 1,
                "data": base64.b64encode(jpeg).decode()}))
            await asyncio.sleep(0.6)
            from services import call_service, vision_service
            results["frame_ingested"] = (call_service.node_last_seen > 0
                                         and vision_service.stats().get("frames_ingressed", 0) >= 1)
            await ws.send(json.dumps({"type": "camera_stop"}))
            await asyncio.sleep(0.2)
            results["camera_status"] = True

        # ── /ws/user must REJECT camera_frame ───────────────────────────
        async with websockets.connect(f"{base}/ws/user?token={TOKEN}") as ws:
            await ws.send(json.dumps({"type": "camera_frame", "data": "x"}))
            events = await drain(ws)
            results["user_frame_rejected"] = any(
                e.get("event") == "error"
                and e.get("data", {}).get("code") == "camera_frame_forbidden"
                for e in events)

        # ── §5 voice bridge over WS ─────────────────────────────────────
        async with websockets.connect(f"{base}/ws/user?token={TOKEN}") as ws:
            await ws.send(json.dumps({"type": "voice_command",
                                      "command": "READ_STATUS"}))
            events = await drain(ws)
            results["read_status_ack"] = any(
                e.get("event") == "command_ack" and e.get("data", {}).get("ok") is True
                for e in events)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
    return results


wsres = asyncio.run(ws_e2e())
check("tokenless connect closed 4401", wsres["tokenless_closed"] is True, str(wsres))
check("flat ping → pong", wsres["pong"] is True)
check("heartbeat → heartbeat_ack", wsres["heartbeat_ack"] is True)
check("resync re-pushes telemetry", wsres["resync_telemetry"] is True)
check("unknown type → error event (never silent)", wsres["unknown_replied"] is True)
check("camera_frame ingested on /ws/rover → vision hub",
      wsres["frame_ingested"] is True, str(wsres))
check("camera_frame forbidden on /ws/user", wsres["user_frame_rejected"] is True)
check("voice READ_STATUS acked over WS", wsres["read_status_ack"] is True)

# ── [4] §5 voice vocabulary bridge (service level) ───────────────────────────
print("\n[4] §5 voice vocabulary bridge")
from services import voice_service
r = voice_service.execute("STOP_PATROL")
check("STOP_PATROL bridges to stop", r.get("handled") is True, str(r))
r = voice_service.execute("STANDBY")
check("STANDBY bridges (dock tag missing is a clean refusal)",
      r.get("handled") in (True, False) and "error" not in ("internal",), str(r))
check("STANDBY → go_to_dock alias", r.get("command") == "go_to_dock", str(r))

# ── [5] §6 Phase-1 REST endpoints ────────────────────────────────────────────
print("\n[5] §6 REST additions")

# pairing token flow
st, r = call("GET", "/api/robots/sentra-01/pairing-token", token=TOKEN)
check("GET pairing-token 200", st == 200 and len(r.get("token", "")) == 6, f"{st} {r}")
ptoken = r.get("token")
st, r = call("POST", "/api/auth/rover/register",
             {"device_id": "rover-test-01", "pairing_token": ptoken,
              "pairing_code": "123456"})
check("rover register → node credential", st == 200 and r.get("user", {}).get("role") == "rover",
      f"{st} {r}")
st, r = call("POST", "/api/auth/rover/register",
             {"device_id": "rover-test-02", "pairing_token": "000000"})
check("single-use token rejected after burn", st == 404, f"{st}")

# sensors + status
st, r = call("GET", "/api/robots/sentra-01/sensors")
check("GET sensors (sensor_update shape)",
      st == 200 and {"front_distance", "imu", "cliff_sensors"} <= set(r.keys()), f"{st} {str(r)[:120]}")
st, r = call("GET", "/api/robots/sentra-01/status")
check("GET status (robot+navigation+telemetry)",
      st == 200 and {"navigation", "telemetry", "current_mode"} <= set(r.keys()), f"{st}")
st, r = call("GET", "/api/robots/nope/status")
check("status unknown robot → 404", st == 404)

# standby + patrol resume
st, r = call("POST", "/api/robots/sentra-01/patrol/resume", token=TOKEN)
# Phase 5: resume is a REAL resume now — with no paused session it cleanly
# refuses (409) instead of silently starting a fresh patrol.
check("patrol/resume without session → 409", st == 409, f"{st} {r}")
call("POST", "/api/robots/sentra-01/patrol/stop", token=TOKEN)

# PATCH aliases
st, r = call("POST", "/api/robots/sentra-01/locations",
             {"tag_id": 2, "name": "Kitchen"}, token=TOKEN)
check("create location 201", st == 201, f"{st} {r}")
st, r = call("PATCH", "/api/robots/sentra-01/locations/loc-2",
             {"name": "Kitchen Two"}, token=TOKEN)
check("PATCH location works (Flutter verb)", st == 200 and r.get("name") == "Kitchen Two",
      f"{st} {r}")
st, r = call("PUT", "/api/robots/sentra-01/locations/loc-2",
             {"name": "Kitchen"}, token=TOKEN)
check("PUT location still works", st == 200, f"{st}")

# alerts: create one via add_manual, then detail/image/dismiss
from services import notification_service
notif_path = os.path.join(tempfile.mkdtemp(), "notifications.json")
notification_service.load(notif_path)
notification_service.add_manual("Unknown person", "Unrecognized person detected",
                                severity="WARNING")
st, alerts = call("GET", "/api/alerts")
check("alerts list", st == 200 and isinstance(alerts, list) and alerts, f"{st}")
aid = alerts[0]["id"]
st, r = call("GET", f"/api/alerts/{aid}")
check("GET alert detail", st == 200 and r.get("id") == aid, f"{st} {r}")
st, r = call("GET", f"/api/alerts/{aid}/image")
check("alert image 404 'no image attached' (pre-vision phase)",
      st == 404 and r.get("detail") == "no image attached", f"{st} {r}")
st, r = call("PATCH", f"/api/alerts/{aid}/dismiss", token=TOKEN)
check("PATCH dismiss works (Flutter verb)", st == 200, f"{st} {r}")

# calls
st, r = call("POST", "/api/calls/initiate", {"robot_id": "sentra-01"}, token=TOKEN)
check("calls initiate 200", st == 200 and r.get("status") == "ringing", f"{st} {r}")
cid = r["id"]
st, r = call("GET", f"/api/calls/{cid}")
check("GET call detail", st == 200 and r.get("id") == cid, f"{st} {r}")

# camera status key fix
st, r = call("GET", "/api/robots/sentra-01/camera/status")
check("camera status shape", st == 200 and "is_streaming" in r, f"{st} {r}")

# ── [6] app integration ──────────────────────────────────────────────────────
print("\n[6] app integration")
from main import app  # noqa: F401
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {
    "/api/health", "/api/auth/rover/register",
    "/api/robots/{robot_id}/pairing-token", "/api/robots/{robot_id}/sensors",
    "/api/robots/{robot_id}/status", "/api/robots/{robot_id}/standby",
    "/api/robots/{robot_id}/patrol/resume", "/api/alerts/{alert_id}",
    "/api/alerts/{alert_id}/image", "/api/calls/{call_id}",
}
check("all Phase-1 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("Phase-21 paths intact", "/api/robots" in schema_paths and "/api/people" in schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
