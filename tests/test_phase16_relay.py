"""
SENTRA — Phase 16 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase16_relay.py

Covers: local ASGI dispatch bridge (auth applies to tunnel traffic too),
relay client lifecycle + status, end-to-end through the REAL reference relay
server (unit dials in, app binds, proxied GET/POST, DANGER push delivery),
router handlers, app routes.
"""

import asyncio
import base64
import importlib.util
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
_dpath = os.path.join(tempfile.mkdtemp(), "devices.json")
os.environ["SENTRA_DEVICES_PATH"] = _dpath

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
import services.device_registry as dr
importlib.reload(dr)
dr.load()
import core.auth as ca
importlib.reload(ca)

import services.relay_client as rc
importlib.reload(rc)

print("\n[2] disabled state + status shape")
check("disabled without SENTRA_RELAY_URL", rc.configure_from_env() is False)
st = rc.get_status()
check("status shape", st["enabled"] is False and st["connected"] is False
      and st["unit_id"] and "url" in st, str(st))
rc.start()   # must no-op cleanly
check("start() no-op when disabled", rc._task is None)

print("\n[3] local ASGI dispatch bridge")
from main import app  # noqa: F401  (dispatch imports this lazily; warm it)


def b2j(b64):
    return json.loads(base64.b64decode(b64))


r = asyncio.run(rc.dispatch_local("GET", "/api/v1/ping", None, {}, b""))
check("proxied GET /ping → 200", r["status"] == 200, str(r["status"]))
check("proxied body decodes", b2j(r["body_b64"]).get("status") == "online",
      str(b2j(r["body_b64"])))
r = asyncio.run(rc.dispatch_local(
    "POST", "/api/v1/control/estop", None,
    {"content-type": "application/json"}, b"{}"))
check("tunnel POST unauthenticated → 401 (auth applies)", r["status"] == 401,
      str(r["status"]))
r = asyncio.run(rc.dispatch_local("GET", "/api/v1/relay/status", None, {}, b""))
check("proxied relay status → 200", r["status"] == 200)
r = asyncio.run(rc.dispatch_local("GET", "/api/v1/nope", None, {}, b""))
check("unknown path → 404", r["status"] == 404, str(r["status"]))

print("\n[4] end-to-end through the REAL reference relay server")
spec = importlib.util.spec_from_file_location(
    "sentra_relay_server",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "relay_server.py"))
relay_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_mod)

import websockets


async def e2e():
    server = await websockets.serve(relay_mod.relay, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    results = {}

    os.environ["SENTRA_RELAY_URL"] = f"ws://127.0.0.1:{port}"
    rc.configure_from_env()
    rc.attach_loop(asyncio.get_running_loop())
    rc.start()
    for _ in range(100):
        if rc.get_status()["connected"]:
            break
        await asyncio.sleep(0.05)
    results["connected"] = rc.get_status()["connected"]

    async with websockets.connect(f"ws://127.0.0.1:{port}") as app_ws:
        await app_ws.send(json.dumps({"type": "app_hello",
                                      "unit_id": rc.UNIT_ID}))
        ack = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=5))
        results["app_hello"] = ack.get("type")

        # proxied GET through relay → unit → local ASGI → back
        await app_ws.send(json.dumps({"type": "http", "id": "r1",
                                      "method": "GET", "path": "/api/v1/ping",
                                      "query": {}, "headers": {},
                                      "body_b64": ""}))
        resp = None
        for _ in range(50):
            msg = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=5))
            if msg.get("type") == "http_response" and msg.get("id") == "r1":
                resp = msg
                break
        results["proxied_status"] = resp.get("status") if resp else None
        results["proxied_body"] = b2j(resp["body_b64"]) if resp else None

        # unauthenticated proxied POST → unit middleware 401
        await app_ws.send(json.dumps(
            {"type": "http", "id": "r2", "method": "POST",
             "path": "/api/v1/control/estop", "query": {},
             "headers": {"content-type": "application/json"},
             "body_b64": base64.b64encode(b"{}").decode()}))
        resp2 = None
        for _ in range(50):
            msg = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=5))
            if msg.get("type") == "http_response" and msg.get("id") == "r2":
                resp2 = msg
                break
        results["proxied_post_status"] = resp2.get("status") if resp2 else None

        # DANGER safety event → spontaneous push to the bound app
        import services.safety_events as se
        se.report("CLIFF", {"side": "front"}, severity="DANGER")
        push = None
        try:
            for _ in range(50):
                msg = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=5))
                if msg.get("type") == "push":
                    push = msg
                    break
        except asyncio.TimeoutError:
            pass
        results["push"] = push

    rc.stop()
    if rc._task is not None:
        rc._task.cancel()
        try:
            await rc._task
        except (asyncio.CancelledError, Exception):
            pass
    server.close()
    await server.wait_closed()
    return results


import services.safety_events as se
se.add_listener(rc._on_safety_event)   # lifespan wires this in production

res = asyncio.run(e2e())
check("unit tunnel connected", res["connected"] is True, str(res))
check("app handshake hello_ok", res["app_hello"] == "hello_ok", str(res))
check("proxied GET /ping 200 end-to-end", res["proxied_status"] == 200,
      str(res["proxied_status"]))
check("proxied body intact", res["proxied_body"]
      and res["proxied_body"].get("status") == "online", str(res["proxied_body"]))
check("tunnel POST unauthenticated → 401 end-to-end",
      res["proxied_post_status"] == 401, str(res["proxied_post_status"]))
check("DANGER event pushed to remote app",
      res["push"] and res["push"]["payload"].get("kind") == "safety_event"
      and res["push"]["payload"].get("event") == "CLIFF", str(res["push"]))
os.environ.pop("SENTRA_RELAY_URL", None)
rc.configure_from_env()

print("\n[5] router handlers")
from routers.relay import relay_status, relay_probe
from models.relay import RelayProbeRequest

s = relay_status()
check("GET /relay/status handler", s["enabled"] is False and "unit_id" in s)
p = relay_probe(RelayProbeRequest(method="GET", path="/api/v1/ping"))
check("POST /relay/probe self-test", p.ok is True and p.status == 200, str(p))
p2 = relay_probe(RelayProbeRequest(method="POST", path="/api/v1/control/estop"))
check("probe honors auth (401 through bridge)", p2.ok is True
      and p2.status == 401, str(p2))

print("\n[6] app integration")
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/relay/status", "/api/v1/relay/probe"}
check("all Phase 16 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("earlier phases intact",
      {"/api/v1/devices", "/api/v1/auth/refresh", "/api/v1/notifications"}
      <= schema_paths)
check("path count >= 89 (Phase 17 owns exact count)",
      len(schema_paths) >= 89, str(len(schema_paths)))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
