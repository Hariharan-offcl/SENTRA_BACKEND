"""
SENTRA — Phase 4 security hardening tests.

Run:
    python -X utf8 tests/test_phase4_security.py

Proves:
  * /api/auth/login enforces SENTRA_PASSWORD when configured (401 otherwise),
  * /api/auth/rover/pair + /api/auth/rover/register enforce the pairing code,
  * /ws/telemetry, /ws/alerts, /ws/call/user, /ws/webrtc/user all refuse
    tokenless connections with 4401 when enforcement is on,
  * CORS is no longer the wildcard+credentials combo,
  * auth stays cookie-free (bearer tokens only).
"""

import asyncio
import base64
import json
import os
import sys
import tempfile

os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")
os.environ["SENTRA_PASSWORD"] = "s3cret-pass"
os.environ["SENTRA_PAIRING_CODE"] = "654321"
os.environ.pop("SENTRA_CORS_ORIGINS", None)

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
import services.device_registry as dr
dr.load()
import core.auth as ca
import services.compat_map as cm
import services.relay_client as rc

check("password loaded from env", ca.APP_PASSWORD == "s3cret-pass")
check("pairing code loaded from env", ca.PAIRING_CODE == "654321")
check("check_password rejects wrong", ca.check_password("wrong") is False)
check("check_password accepts right", ca.check_password("s3cret-pass") is True)


def call(method, path, body=None, token=None, headers_extra=None):
    headers = dict(headers_extra or {})
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


print("\n[2] password gate on /api/auth/login")
st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
check("wrong password → 401", st == 401, f"{st} {r}")
st, r = call("POST", "/api/auth/login", {"username": "admin"})
check("missing password → 401", st == 401, f"{st}")
st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "s3cret-pass"})
check("correct password → 200 + token", st == 200 and r.get("token"), f"{st}")
TOKEN = r["token"]

print("\n[3] pairing-code gate")
st, r = call("POST", "/api/auth/rover/pair",
             {"device_id": "rover-sec-01", "pairing_code": "111111"})
check("wrong pairing code → 403", st == 403, f"{st} {r}")
st, r = call("POST", "/api/auth/rover/pair",
             {"device_id": "rover-sec-01", "pairing_code": "654321"})
check("correct pairing code → 200", st == 200 and r.get("token"), f"{st}")
ROVER_TOKEN = r["token"]
st, pairing = call("GET", "/api/robots/sentra-01/pairing-token", token=TOKEN)
st, r = call("POST", "/api/auth/rover/register",
             {"device_id": "rover-sec-02", "pairing_token": pairing["token"],
              "pairing_code": "000000"})
check("register with wrong code → 403", st == 403, f"{st} {r}")
st, r = call("POST", "/api/auth/rover/register",
             {"device_id": "rover-sec-02", "pairing_token": pairing["token"],
              "pairing_code": "654321"})
check("register with correct code → 200", st == 200 and r.get("token"), f"{st}")

print("\n[4] WS token gates on the legacy sockets")
import websockets
import uvicorn
import socket
from main import app

port_holder = {}


async def ws_checks():
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
    out = {}
    base = f"ws://127.0.0.1:{port_holder['port']}"
    paths = ["/ws/telemetry", "/ws/alerts", "/ws/call/user", "/ws/webrtc/user"]
    try:
        for p in paths:
            try:
                async with websockets.connect(f"{base}{p}") as ws:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                out[p] = "accepted"
            except Exception as exc:
                out[p] = "4401" if "4401" in str(exc) else str(exc)
        # With a token, telemetry must connect and stream.
        try:
            async with websockets.connect(f"{base}/ws/telemetry?token={TOKEN}") as ws:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                out["telemetry_with_token"] = isinstance(msg, dict)
        except Exception as exc:
            out["telemetry_with_token"] = f"error: {exc}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
    return out


res = asyncio.run(ws_checks())
for p in ("/ws/telemetry", "/ws/alerts", "/ws/call/user", "/ws/webrtc/user"):
    check(f"{p} tokenless → 4401", res.get(p) == "4401", str(res.get(p)))
check("telemetry WITH token streams", res.get("telemetry_with_token") is True,
      str(res.get("telemetry_with_token")))

print("\n[5] CORS + cookie-free auth")
from fastapi.middleware.cors import CORSMiddleware
cors = next(m for m in app.user_middleware
            if m.cls is CORSMiddleware)
check("allow_credentials is False", cors.kwargs.get("allow_credentials") is False,
      str(cors.kwargs))
check("origins default wildcard without credentials",
      cors.kwargs.get("allow_origins") == ["*"], str(cors.kwargs.get("allow_origins")))

# No Set-Cookie anywhere in the auth flow
st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "s3cret-pass"})
r2 = asyncio.run(rc.dispatch_local(
    "POST", "/api/auth/login", None,
    {"content-type": "application/json"},
    json.dumps({"username": "admin", "password": "s3cret-pass"}).encode()))
cookie_headers = [k for k in r2["headers"] if k.lower() == "set-cookie"]
check("auth flow sets no cookies", cookie_headers == [], str(r2["headers"]))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
