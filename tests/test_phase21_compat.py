"""
SENTRA — Phase 21 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase21_compat.py

Covers: compat auth (login/rover-pair/me/logout), robot object + mode change,
control REST (move/stop/brake/estop via motion controller), locations CRUD,
navigate/go-to, patrol routes + start/pause/stop, dock, alerts flat array +
dismiss, people multipart register/update/delete, calls lifecycle, the
multiplexed /ws (envelope, ping→pong, control_move, telemetry/sensor push,
e-stop), safety→alert/emergency push, and app integration (route count).
"""

import asyncio
import base64
import importlib
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


print("\n[1] setup")
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")

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
importlib.reload(dr)
dr.load()
import core.auth as ca
importlib.reload(ca)
import services.compat_map as cm
importlib.reload(cm)

import services.relay_client as rc  # dispatch bridge for HTTP-level E2E


def call(method, path, body=None, token=None, raw_body=None, content_type=None):
    headers = {}
    body_b64 = ""
    if body is not None:
        body_b64 = base64.b64encode(json.dumps(body).encode()).decode()
        headers["content-type"] = "application/json"
    if raw_body is not None:
        body_b64 = base64.b64encode(raw_body).decode()
        headers["content-type"] = content_type
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = asyncio.run(rc.dispatch_local(method, path, None, headers, body_b64 and base64.b64decode(body_b64)))
    try:
        parsed = json.loads(base64.b64decode(r["body_b64"])) if r["body_b64"] else {}
    except Exception:
        parsed = {}
    return r["status"], parsed


print("\n[2] compat auth")
st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "x"})
check("login 200", st == 200, f"{st} {r}")
check("login token + user", r.get("token")
      and r["user"]["role"] == "admin"
      and r["user"]["username"] == "admin", str(r))
TOKEN = r["token"]
check("token verifies via core.auth", ca.verify_token(TOKEN).role == "OWNER")

st, r = call("POST", "/api/auth/login", {"username": "granny_app", "role": "user"})
check("user-role login", st == 200 and r["user"]["role"] == "user")
USER_TOKEN = r["token"]
check("user role maps to GUARD", ca.verify_token(USER_TOKEN).role == "GUARD")

st, r = call("POST", "/api/auth/rover/pair",
             {"robot_id": "sentra-01", "device_id": "rover-cam-01",
              "pairing_code": "123456"})
check("rover pair 200", st == 200, f"{st} {r}")
check("rover role + robot_id", r["user"]["role"] == "rover"
      and r["user"]["robot_id"] == "sentra-01", str(r))
ROVER_TOKEN = r["token"]
check("rover token kind=node", ca.verify_token(ROVER_TOKEN).kind == "node")

st, r = call("GET", "/api/auth/me", token=TOKEN)
check("me returns user object", st == 200 and r["username"] == "admin", f"{st} {r}")
st, r = call("GET", "/api/auth/me")
check("me without token → 401", st == 401, str(st))
st, r = call("GET", "/api/auth/me", token="garbage")
check("me with bad token → 401", st == 401, str(st))

st, r = call("POST", "/api/auth/logout", token=USER_TOKEN)
check("logout 200 {}", st == 200 and r == {}, f"{st} {r}")
st, r = call("GET", "/api/auth/me", token=USER_TOKEN)
check("token revoked after logout", st == 401, str(st))

print("\n[3] robots + modes")
st, r = call("GET", "/api/robots")
check("robot list shape", st == 200 and r[0]["id"] == "sentra-01"
      and r[0]["status"] == "online" and r[0]["current_mode"] in
      ("idle", "manual", "mapping", "patrol", "auto", "docking"), str(r)[:200])
st, r = call("GET", "/api/robots/sentra-01")
check("single robot", st == 200 and r["id"] == "sentra-01")
st, r = call("GET", "/api/robots/nope")
check("unknown robot → 404", st == 404)

st, r = call("POST", "/api/robots/sentra-01/mode", {"mode": "manual"}, token=TOKEN)
check("mode → manual", st == 200 and r["current_mode"] == "manual", f"{st} {r}")
st, r = call("GET", "/api/robots/sentra-01/mode")
check("GET mode (Flutter probe, no token)", st == 200 and "mode" in r, f"{st} {r}")
st, r = call("PATCH", "/api/robots/sentra-01/mode", {"mode": "patrol"}, token=TOKEN)
check("PATCH mode (Flutter verb)", st == 200, f"{st} {r}")
call("POST", "/api/robots/sentra-01/patrol/stop", token=TOKEN)
st, r = call("PUT", "/api/robots/sentra-01/mode", {"mode": "idle"}, token=TOKEN)
check("PUT mode accepted too", st == 200 and r["mode"] == "idle", f"{st} {r}")
st, r = call("PATCH", "/api/robots/sentra-01/mode", {"mode": "idle"})
check("PATCH mode without token → 401", st == 401, str(st))
st, r = call("POST", "/api/robots/sentra-01/mode", {"mode": "bogus"}, token=TOKEN)
check("bad mode → 400", st == 400)
st, r = call("POST", "/api/robots/sentra-01/mode", {"mode": "manual"})
check("mode change unauthenticated → 401", st == 401)

print("\n[4] control REST")
st, r = call("POST", "/api/robots/sentra-01/control/move",
             {"left_speed": 0.5, "right_speed": 0.5}, token=TOKEN)
check("control/move 200 (motion controller adopted MANUAL)", st == 200, f"{st} {r}")
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
stt = mc.get_state()
check("move reached the ramp loop", stt.get("target_left") == 50.0, str(stt))
st, r = call("POST", "/api/robots/sentra-01/control/stop", {}, token=TOKEN)
check("control/stop 200", st == 200)
st, r = call("POST", "/api/robots/sentra-01/control/brake", {}, token=TOKEN)
check("control/brake 200", st == 200)
st, r = call("POST", "/api/robots/sentra-01/control/estop", {}, token=TOKEN)
check("control/estop 200 + estop_active", st == 200
      and r["estop_active"] is True, f"{st} {r}")
st, r = call("POST", "/api/v1/robot/estop/reset", {}, token=TOKEN)
check("estop reset via internal API", st == 200, f"{st} {r}")

print("\n[5] locations")
st, r = call("GET", "/api/robots/sentra-01/locations")
check("locations list from tag map", st == 200 and any(
    loc["name"] == "Kitchen" and loc["id"] == "loc-2" and "tag_id" in loc
    for loc in r), str(r)[:200])
st, r = call("POST", "/api/robots/sentra-01/locations",
             {"tag_id": 7, "name": "Porch", "description": "front door"}, token=TOKEN)
check("create location 201", st == 201 and r["id"] == "loc-7"
      and r["description"] == "front door", f"{st} {r}")
st, r = call("PUT", "/api/robots/sentra-01/locations/loc-7",
             {"name": "Front Porch"}, token=TOKEN)
check("rename location", st == 200 and r["name"] == "Front Porch", f"{st} {r}")
st, r = call("DELETE", "/api/robots/sentra-01/locations/loc-7", token=TOKEN)
check("delete location 204", st == 204, str(st))

print("\n[6] navigate/go-to + patrol + dock")
st, r = call("POST", "/api/robots/sentra-01/navigate/go-to",
             {"location_id": "loc-2"}, token=TOKEN)
check("go-to starts (or refused cleanly in sim)", st in (200, 409), f"{st} {r}")
st, r = call("POST", "/api/robots/sentra-01/navigate/go-to",
             {"location_id": "loc-999"}, token=TOKEN)
check("go-to unknown → 404", st == 404)

st, r = call("POST", "/api/robots/sentra-01/patrol/routes",
             {"name": "Night Round", "waypoints": ["loc-2", "loc-3"],
              "is_looping": True}, token=TOKEN)
check("create route 201 (loc ids resolved)", st == 201
      and r["waypoints"] == ["loc-2", "loc-3"] and r["is_looping"] is True,
      f"{st} {r}")
st, r = call("GET", "/api/robots/sentra-01/patrol/routes")
check("list routes", st == 200 and any(rt["name"] == "Night Round" for rt in r))
st, r = call("POST", "/api/robots/sentra-01/patrol/start",
             {"route_id": "route-Night Round"}, token=TOKEN)
check("patrol start (or refused if no fresh vision)", st in (200, 409), f"{st} {r}")
call("POST", "/api/robots/sentra-01/patrol/stop", {}, token=TOKEN)
st, r = call("POST", "/api/robots/sentra-01/patrol/pause", token=TOKEN)
check("patrol pause → stop semantics", st == 200 and "stopped" in r, f"{st} {r}")
st, r = call("DELETE", "/api/robots/sentra-01/patrol/routes/route-Night Round",
             token=TOKEN)
check("delete route", st == 200, f"{st} {r}")

st, r = call("POST", "/api/robots/sentra-01/dock", token=TOKEN)
check("dock start (or refused without DOCK tag visible)", st in (200, 409), f"{st} {r}")
st, r = call("POST", "/api/robots/sentra-01/dock/cancel", token=TOKEN)
check("dock cancel 200", st == 200)

print("\n[7] alerts")
import services.notification_service as ns
importlib.reload(ns)
ns.load(os.path.join(tempfile.mkdtemp(), "notifs.json"))
n1 = ns.add_manual("Emergency: Fall detected", "test fall", "DANGER", "Kitchen")
n2 = ns.add_manual("Patrol update", "test info", "INFO", "Hall")
st, r = call("GET", "/api/alerts")
check("alerts flat array", isinstance(r, list) and len(r) >= 2, str(r)[:200])
a1 = next((a for a in r if a["id"] == n1["id"]), None)
check("alert object shape", a1 and a1["type"] == "fall" and a1["severity"] == "critical"
      and a1["robot_id"] == "sentra-01" and a1["status"] == "active"
      and a1["timestamp"].endswith("Z"), str(a1))
check("INFO maps to info severity", any(a["severity"] == "info" for a in r))
st, r = call("POST", f"/api/alerts/{n1['id']}/dismiss", token=TOKEN)
check("dismiss 200 {}", st == 200 and r == {}, f"{st} {r}")
st, r = call("GET", "/api/alerts")
check("dismissed status reflects", next(a for a in r if a["id"] == n1["id"])
      ["status"] == "dismissed")
st, r = call("POST", "/api/alerts/NOTIF-nope/dismiss", token=TOKEN)
check("dismiss unknown → 404", st == 404)

print("\n[8] people (multipart)")
import cv2
import numpy as np
img = np.full((120, 120, 3), 200, dtype=np.uint8)
cv2.circle(img, (60, 60), 30, (90, 90, 90), -1)
ok, buf = cv2.imencode(".jpg", img)
JPEG = buf.tobytes()


def multipart(fields, boundary="xxsentraxx"):
    out = b""
    for k, v in fields.items():
        if isinstance(v, bytes):
            out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f"name=\"{k}\"; filename=\"f.jpg\"\r\n"
                    f"Content-Type: image/jpeg\r\n\r\n").encode() + v + b"\r\n"
        else:
            out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


body, ctype = multipart({"name": "John Doe", "notes": "Primary resident", "image": JPEG})
st, r = call("POST", "/api/people", raw_body=body, content_type=ctype, token=TOKEN)
check("register 201 (or 400 when dev embedder has no face)",
      st in (201, 400), f"{st} {r}")
if st == 201:
    pid = r["id"]
    body, ctype = multipart({"notes": "Updated notes"}, )
    st, r = call("PUT", f"/api/people/{pid}", raw_body=body, content_type=ctype,
                 token=TOKEN)
    check("update person notes", st == 200 and r["notes"] == "Updated notes",
          f"{st} {r}")
    st, r = call("GET", "/api/people")
    check("people list", st == 200 and any(p["id"] == pid for p in r))
    st, r = call("DELETE", f"/api/people/{pid}", token=TOKEN)
    check("delete person 204", st == 204, str(st))
else:
    check("dev-machine face detect absent is tolerated", "no face" in r.get("detail", ""))

print("\n[9] calls lifecycle")
st, r = call("POST", "/api/calls/initiate", {"robot_id": "sentra-01"}, token=TOKEN)
check("initiate 200 + ringing", st == 200 and r["status"] == "ringing", f"{st} {r}")
cid = r["id"]
import ws_handlers.compat_ws as cws
pushed = []


class _FakeWS:
    async def send_text(self, s):
        pushed.append(json.loads(s))


cws._clients.add(_FakeWS())  # type: ignore
try:
    st, r = call("POST", f"/api/calls/{cid}/accept", token=TOKEN)
    check("accept → active", st == 200 and r["status"] == "active")

    async def _end_and_collect():
        # push_event needs a live loop + client; run the end-call in the same loop
        loop = asyncio.get_running_loop()
        cws._alert_loop = loop
        cws._clients.add(_FakeWS())
        await asyncio.sleep(0.05)
        headers = {"authorization": f"Bearer {TOKEN}"}
        rr = await rc.dispatch_local("POST", f"/api/calls/{cid}/end", None,
                                     headers, b"")
        await asyncio.sleep(0.3)  # let the scheduled push run
        cws._clients.clear()
        cws._alert_loop = None
        return rr["status"], json.loads(base64.b64decode(rr["body_b64"]))

    st, r = asyncio.run(_end_and_collect())
    check("end → ended", st == 200 and r["status"] == "ended")
    check("call_ended pushed to ws clients",
          any(p.get("event") == "call_ended" and p["data"]["callId"] == cid
              for p in pushed), str(pushed))
    st, r = call("POST", "/api/calls/call-nope/accept", token=TOKEN)
    check("unknown call → 404", st == 404)
finally:
    cws._clients.clear()

print("\n[10] multiplexed /ws — envelope, ping, control, pushes")
import websockets

port_holder = {}


async def ws_e2e():
    from main import app
    import uvicorn
    import socket
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
    try:
        async with websockets.connect(
                f"ws://127.0.0.1:{port_holder['port']}/ws?token={TOKEN}") as ws:
            await ws.send(json.dumps({"event": "ping"}))
            got_pong = False
            telemetry = None
            for _ in range(40):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if msg.get("event") == "pong":
                    got_pong = True
                elif msg.get("event") == "telemetry":
                    telemetry = msg
                    if got_pong and telemetry:
                        break
            results["pong"] = got_pong
            results["telemetry"] = telemetry
            results["envelope"] = (set(telemetry.keys()) == {"event", "data"}
                                   if telemetry else False)

            await ws.send(json.dumps({"event": "control_move",
                                      "data": {"left_speed": 0.4,
                                               "right_speed": 0.4}}))
            await asyncio.sleep(0.4)
            st_state = mc.get_state()
            results["move_applied"] = st_state.get("target_left") == 40.0
            await ws.send(json.dumps({"event": "control_stop", "data": {}}))

            # navigation_status arrives when a session exists (may be idle here)
            results["nav_event_ok"] = True
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
    return results


wsres = asyncio.run(ws_e2e())
check("WS: ping → pong", wsres["pong"] is True)
check("WS: telemetry event in {event,data} envelope", wsres["envelope"] is True,
      str(wsres["telemetry"])[:200])
check("WS: telemetry data fields", wsres["telemetry"] and
      {"battery_percent", "mode", "connection_status", "is_emergency",
       "current_location"} <= set(wsres["telemetry"]["data"]),
      str(wsres["telemetry"]))
check("WS: control_move drove the motion controller", wsres["move_applied"] is True,
      str(mc.get_state()))

print("\n[11] safety → alert/emergency push")
import services.safety_events as se
alerts_out = []


class _CapWS:
    async def send_text(self, s):
        alerts_out.append(json.loads(s))


async def _fire():
    cws._clients.add(_CapWS())
    cws._alert_loop = asyncio.get_running_loop()
    await asyncio.sleep(0.05)
    se.report("FALL", {"person_id": 42, "confidence": 0.95}, severity="DANGER")
    await asyncio.sleep(0.5)
    cws._clients.clear()


asyncio.run(_fire())
kinds = [p.get("event") for p in alerts_out]
check("alert pushed on safety event", "alert" in kinds, str(kinds))
check("emergency pushed for DANGER", "emergency" in kinds, str(kinds))
alert_data = next((p["data"] for p in alerts_out if p.get("event") == "alert"), None)
check("alert data shape (type fall, severity critical)",
      alert_data and alert_data["type"] == "fall" and alert_data["severity"] == "critical",
      str(alert_data))

print("\n[12] app integration")
from main import app  # noqa: F401
schema_paths = set(app.openapi()["paths"].keys())
_new = {"/api/auth/login", "/api/auth/rover/pair", "/api/auth/me",
        "/api/robots", "/api/alerts", "/api/people", "/api/calls/initiate"}
check("compat paths registered", _new <= schema_paths,
      f"missing: {_new - schema_paths}")
check("internal /api/v1 paths intact",
      "/api/v1/system/status" in schema_paths and "/api/v1/ping" in schema_paths)
check("path count now 121", len(schema_paths) == 121, str(len(schema_paths)))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
