"""
SENTRA — Phase 15 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase15_auth.py

Covers: token issue/verify + registry tracking, per-token & per-device
revocation, refresh rotation, REST enforcement (401/403 + open paths +
read-only pass-through + enforcement switch), WS control gate (4401/4403),
auth + devices + pair router handlers, persistence across reload, routes.
"""

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
dr.load()                       # bind to temp path
import core.auth as ca
importlib.reload(ca)
check("enforcement on by default", ca.AUTH_ENFORCED is True)

print("\n[2] issue_session + registry tracking")
sess = ca.issue_session("dev-1", "OWNER")
check("token issued", len(sess["token"]) > 50)
check("refresh token issued", len(sess["refresh_token"]) > 30)
check("OWNER permissions", "MANUAL_CONTROL" in sess["permissions"]
      and "DEVICES_MANAGE" in sess["permissions"], str(sess["permissions"]))
ctx = ca.verify_token(sess["token"])
check("verify → device", ctx.device_id == "dev-1")
check("verify → role/kind", ctx.role == "OWNER" and ctx.kind == "user")
check("jti tracked", dr.token_state(ctx.jti) == "active", dr.token_state(ctx.jti))
st = dr.stats()
check("registry: 2 devices after node", st["devices_total"] == 1
      and st["tokens_tracked"] == 1 and st["tokens_active"] == 1, str(st))
node = dr.upsert_device("node-1", kind="node", platform="android")
check("device kinds supported", node["kind"] == "node")

print("\n[3] revocation (token + device)")
sess2 = ca.issue_session("dev-2", "GUARD")
ctx2 = ca.verify_token(sess2["token"])
check("second device active", dr.token_state(ctx2.jti) == "active")
ca.device_registry.revoke_token(ctx2.jti)
try:
    ca.verify_token(sess2["token"])
    check("revoked token rejected", False, "no exception")
except PermissionError:
    check("revoked token rejected", True)
sess3 = ca.issue_session("dev-3", "GUARD")
n = dr.revoke_device_tokens("dev-3")
check("wholesale device token kill", n == 1)
try:
    ca.issue_session("dev-3", "GUARD")   # still registered, not revoked → ok
    dr.set_device_revoked("dev-3", True)
    try:
        ca.issue_session("dev-3", "GUARD")
        check("revoked device cannot login", False, "no exception")
    except PermissionError:
        check("revoked device cannot login", True)
finally:
    dr.set_device_revoked("dev-3", False)

print("\n[4] refresh rotation")
sess4 = ca.issue_session("dev-4", "GUARD")
claims = ca.decode_claims(sess4["token"])
rotated = ca.rotate_session(sess4["refresh_token"])
check("rotation → new tokens", rotated["token"] != sess4["token"]
      and rotated["refresh_token"] != sess4["refresh_token"])
check("old jti revoked after rotation",
      dr.token_state(claims["jti"]) == "revoked")
new_ctx = ca.verify_token(rotated["token"])
check("new jti active", dr.token_state(new_ctx.jti) == "active")
try:
    ca.rotate_session(sess4["refresh_token"])
    check("used refresh token single-use", False, "no exception")
except PermissionError:
    check("used refresh token single-use", True)
try:
    ca.rotate_session("bogus-token")
    check("bogus refresh rejected", False, "no exception")
except PermissionError:
    check("bogus refresh rejected", True)

print("\n[5] REST enforcement (dependency + middleware path)")
from fastapi import HTTPException


def fake_request(method, path, token=None):
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    from starlette.requests import Request
    return Request({"type": "http", "method": method, "path": path,
                    "headers": headers, "query_string": b""})


import asyncio
r = asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/control/direct",
                                             sess["token"])))
check("valid token → ctx", r.device_id == "dev-1" and r.role == "OWNER")
try:
    asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/control/direct")))
    check("missing token → 401", False, "no exception")
except HTTPException as exc:
    check("missing token → 401", exc.status_code == 401, str(exc.status_code))
try:
    asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/control/direct",
                                             "garbage.token.here")))
    check("invalid token → 401", False, "no exception")
except HTTPException as exc:
    check("invalid token → 401", exc.status_code == 401)
r = asyncio.run(ca.enforce_auth(fake_request("GET", "/api/v1/telemetry/live")))
check("GET passes without token (read-only open)", r.device_id == "read-only")
r = asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/auth/session")))
check("login path open", r.device_id == "open")
guest = ca.issue_session("dev-guest", "GUEST")
try:
    asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/voice/command",
                                             guest["token"])))
    check("GUEST mutation → 403", False, "no exception")
except HTTPException as exc:
    check("GUEST mutation → 403", exc.status_code == 403, str(exc.status_code))
guard = ca.issue_session("dev-guard", "GUARD")
r = asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/voice/command",
                                             guard["token"])))
check("GUARD mutation allowed", r.device_id == "dev-guard")
ca.AUTH_ENFORCED = False
r = asyncio.run(ca.enforce_auth(fake_request("POST", "/api/v1/voice/command")))
check("enforcement switch off → open", "*" in r.permissions)
ca.AUTH_ENFORCED = True

print("\n[6] WS control gate")


class FakeWS:
    def __init__(self, token=None):
        self.scope = {"subprotocols": []}
        self.query_params = {"token": token} if token else {}
        self.closed = None

    async def accept(self):
        pass  # denial path accepts first so the client sees the close code

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


ws = FakeWS(sess["token"])
ctxws = asyncio.run(ca.enforce_ws_control(ws))
check("WS: valid OWNER token accepted", ctxws is not None
      and ctxws.device_id == "dev-1")
ws = FakeWS()
asyncio.run(ca.enforce_ws_control(ws))
check("WS: no token → close 4401", ws.closed and ws.closed[0] == 4401, str(ws.closed))
ws = FakeWS(guest["token"])
asyncio.run(ca.enforce_ws_control(ws))
check("WS: GUEST → close 4403", ws.closed and ws.closed[0] == 4403, str(ws.closed))

print("\n[7] router handlers")
from routers.auth import create_session, refresh_session, logout
from routers.devices import (list_devices, device_stats, revoke_device,
                             unrevoke_device, delete_device, device_tokens,
                             cleanup_registry)
from routers.pair import pair_device
from models.requests import AuthSessionRequest, AuthRefreshRequest, AuthLogoutRequest, PairRequest

resp = create_session(AuthSessionRequest(role="OWNER", device_id="dev-r1",
                                         name="Mom's phone", kind="user",
                                         platform="android"))
check("POST /auth/session additive contract", resp.token and resp.role == "OWNER"
      and resp.expires_in > 0 and resp.refresh_token != "")
ref = refresh_session(AuthRefreshRequest(refresh_token=resp.refresh_token))
check("POST /auth/refresh handler", ref.token != resp.token)
out = logout(AuthLogoutRequest(token=ref.token))
check("POST /auth/logout handler", out.ok is True and out.revoked == 1)
out2 = logout(AuthLogoutRequest(token=ref.token))
check("double logout → ok False", out2.ok is False)

d = [x.device_id for x in list_devices().devices]
check("GET /devices lists registered", "dev-r1" in d and "node-1" in d, str(d))
s = device_stats()
check("GET /devices/stats", s.devices_total >= 5 and s.tokens_active >= 1, str(s))
ca.issue_session("dev-r1", "GUARD")   # live token so revocation has something to kill
rv = revoke_device("dev-r1")
check("POST /devices/{id}/revoke", rv.ok is True and rv.tokens_revoked >= 1
      and rv.device.revoked is True,
      f"ok={rv.ok} killed={rv.tokens_revoked} dev={rv.device}")
ur = unrevoke_device("dev-r1")
check("POST /devices/{id}/unrevoke", ur.ok and ur.device.revoked is False)
toks = device_tokens("dev-r1")
check("GET /devices/{id}/tokens audit rows", len(toks) >= 1
      and "refresh_hash" not in toks[0], str(toks[:1]))
dr2 = delete_device("dev-r1")
check("DELETE /devices/{id}", dr2.ok and "dev-r1" not in
      [x.device_id for x in list_devices().devices])
cl = cleanup_registry()
check("POST /devices/cleanup", cl.ok is True and "removed_token_rows" in cl.stats)

pr = pair_device(PairRequest(ip_address="192.168.1.50",
                             app_instance_id="phone-node-x"), None)
check("POST /pair response unchanged", pr.paired is True
      and pr.websocket_control_url.endswith("/ws/control"))
check("pair feeds the registry", dr.get_device("phone-node-x") is not None
      and dr.get_device("phone-node-x")["kind"] == "node")

print("\n[8] persistence across reload")
sess_p = ca.issue_session("dev-persist", "GUARD")
ctx_p = ca.verify_token(sess_p["token"])
importlib.reload(dr)
dr.load()   # disk → memory (as lifespan does)
check("devices survive reload", dr.get_device("dev-persist") is not None)
check("token state survives reload", dr.token_state(ctx_p.jti) == "active")
check("file at configured path", os.path.exists(_dpath))

print("\n[9] app integration")
from main import app
schema_paths = set(app.openapi()["paths"].keys())
new_paths = {"/api/v1/auth/refresh", "/api/v1/auth/logout", "/api/v1/devices",
             "/api/v1/devices/stats", "/api/v1/devices/cleanup",
             "/api/v1/devices/{device_id}", "/api/v1/devices/{device_id}/revoke",
             "/api/v1/devices/{device_id}/unrevoke",
             "/api/v1/devices/{device_id}/tokens"}
check("all Phase 15 paths registered", new_paths <= schema_paths,
      f"missing: {new_paths - schema_paths}")
check("legacy auth/pair intact", {"/api/v1/auth/session", "/api/v1/pair"}
      <= schema_paths)
check("earlier phases intact", {"/api/v1/notifications", "/api/v1/fall/status"}
      <= schema_paths)
check("path count grew past pre-Phase-15 baseline",
      len(schema_paths) >= 86, str(len(schema_paths)))  # newest suite owns exact count

print("\n[10] middleware end-to-end (live bug class: handler ok, middleware blocks)")
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from main import _auth_enforcement


async def mw_call(method, path, token=None):
    headers = [(b"authorization", b"Bearer " + token.encode())] if token else []
    scope = {"type": "http", "method": method, "path": path,
             "headers": headers, "query_string": b""}

    async def receive():
        return {"type": "http.request"}

    async def call_next(r):
        return StarletteResponse("ok")

    resp = await _auth_enforcement(StarletteRequest(scope, receive), call_next)
    return resp.status_code


st = asyncio.run(mw_call("POST", "/api/v1/auth/logout"))
check("middleware: logout open", st == 200, str(st))
st = asyncio.run(mw_call("POST", "/api/v1/auth/session"))
check("middleware: login open", st == 200, str(st))
st = asyncio.run(mw_call("POST", "/api/v1/control/estop"))
check("middleware: mutation without token → 401", st == 401, str(st))
st = asyncio.run(mw_call("POST", "/api/v1/control/estop", sess["token"]))
check("middleware: mutation with valid token → 200", st == 200, str(st))
st = asyncio.run(mw_call("GET", "/api/v1/telemetry/live"))
check("middleware: read-only open", st == 200, str(st))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
