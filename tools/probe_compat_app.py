"""Live probe for the Phase 21 compat layer (run against :8099).

Usage:  PYTHONIOENCODING=utf-8 python tools/probe_compat_app.py
"""
import asyncio
import json
import urllib.request

import websockets

BASE = os_base = "http://127.0.0.1:8099"
WS = "ws://127.0.0.1:8099/ws"


def http(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    print("── login ──")
    st, r = http("POST", "/api/auth/login", {"username": "admin", "password": "x"})
    print(st, {k: r[k] for k in ("user",) if k in r})
    tok = r["token"]

    print("── rover pair ──")
    st, r = http("POST", "/api/auth/rover/pair",
                 {"robot_id": "sentra-01", "device_id": "rover-cam-01",
                  "pairing_code": "123456"})
    print(st, r["user"])

    print("── robots / me ──")
    print(http("GET", "/api/robots", token=tok))
    print(http("GET", "/api/auth/me", token=tok))

    print("── mode + estop fallback ──")
    print(http("POST", "/api/robots/sentra-01/mode", {"mode": "manual"}, token=tok))
    print(http("POST", "/api/robots/sentra-01/control/estop", token=tok))

    print("── alerts / people ──")
    st, alerts = http("GET", "/api/alerts", token=tok)
    print(st, f"{len(alerts)} alerts; first:", alerts[0] if alerts else None)
    print(http("GET", "/api/people", token=tok))

    print("── calls ──")
    st, call = http("POST", "/api/calls/initiate", {"robot_id": "sentra-01"}, token=tok)
    print(st, call)
    print(http("POST", f"/api/calls/{call['id']}/end", token=tok))

    return tok


async def ws_probe(token):
    async with websockets.connect(f"{WS}?token={token}") as ws:
        await ws.send(json.dumps({"event": "ping"}))
        seen = {}
        for _ in range(30):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=4))
            seen[msg["event"]] = msg["data"]
            if msg["event"] == "pong":
                break
        await ws.send(json.dumps({"event": "control_move",
                                  "data": {"left_speed": 0.3, "right_speed": 0.3}}))
        await ws.send(json.dumps({"event": "control_stop", "data": {}}))
        for _ in range(10):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=4))
            seen.setdefault(msg["event"], msg["data"])
            if msg["event"] == "sensor_update":
                break
        for ev in ("pong", "telemetry", "sensor_update"):
            print(f"  {ev}:", json.dumps(seen.get(ev))[:160])


if __name__ == "__main__":
    _token = main()
    asyncio.run(ws_probe(_token))
