"""
SENTRA — Reference cloud relay server (Phase 16, dev tool).

Minimal broker implementation of the §32 envelope protocol. Run this on any
VPS / cloud VM; the unit and the remote app both dial OUT to it.

    python tools/relay_server.py --port 8765 [--secret sentra-relay-dev-secret]

Wire protocol (JSON over WebSocket):
    unit : {"type":"unit_hello","unit_id":…,"secret":…} → {"type":"hello_ok"}
    app  : {"type":"app_hello","unit_id":…}             → {"type":"hello_ok"}
           (if the unit is offline → {"type":"unit_offline"}, then close)
    app  → relay → unit : {"type":"http","id":…,"method":…,"path":…,
                            "query":{…},"headers":{…},"body_b64":…}
    unit → relay → app  : {"type":"http_response","id":…,"status":…,
                            "headers":{…},"body_b64":…,"truncated":bool}
    unit → relay → app  : {"type":"push","payload":{…}}   (spontaneous)

Scaling notes for production: replace the in-memory dicts with Redis pub/sub,
run N stateless replicas behind an LB, terminate TLS at the LB, add per-unit
rate limits. The unit/app protocol stays identical.
"""

import argparse
import asyncio
import json
import logging
import os

import websockets

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("relay")

SECRET = os.getenv("SENTRA_RELAY_UNIT_SECRET", "sentra-relay-dev-secret")
UNITS = {}   # unit_id → unit websocket
APPS = {}    # unit_id → set of app websockets


async def _send_json(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload))


async def relay(ws) -> None:
    unit_id = None
    role = None
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if first.get("type") == "unit_hello":
            if first.get("secret") != SECRET:
                await _send_json(ws, {"type": "hello_error",
                                      "detail": "bad secret"})
                return
            unit_id = first.get("unit_id", "unknown")
            role = "unit"
            UNITS[unit_id] = ws
            await _send_json(ws, {"type": "hello_ok"})
            log.info("unit %s connected (%d online)", unit_id, len(UNITS))
        elif first.get("type") == "app_hello":
            unit_id = first.get("unit_id")
            role = "app"
            if unit_id not in UNITS:
                await _send_json(ws, {"type": "unit_offline"})
                return
            APPS.setdefault(unit_id, set()).add(ws)
            await _send_json(ws, {"type": "hello_ok"})
            log.info("app bound to %s (%d app(s))", unit_id, len(APPS[unit_id]))
        else:
            return

        async for raw in ws:
            try:
                env = json.loads(raw)
            except Exception:
                continue
            etype = env.get("type")
            if role == "unit":
                # unit → app(s): responses and spontaneous pushes
                if etype in ("http_response", "push", "pong"):
                    for app_ws in list(APPS.get(unit_id, ())):
                        try:
                            await app_ws.send(raw)
                        except Exception:
                            APPS.get(unit_id, set()).discard(app_ws)
            elif role == "app":
                # app → unit: proxied HTTP requests
                if etype == "http":
                    unit_ws = UNITS.get(unit_id)
                    if unit_ws is None:
                        await _send_json(ws, {"type": "unit_offline"})
                    else:
                        try:
                            await unit_ws.send(raw)
                        except Exception:
                            UNITS.pop(unit_id, None)
                            await _send_json(ws, {"type": "unit_offline"})
    except Exception as exc:
        log.debug("connection ended: %s", exc)
    finally:
        if role == "unit" and UNITS.get(unit_id) is ws:
            del UNITS[unit_id]
            log.info("unit %s disconnected (%d online)", unit_id, len(UNITS))
        if role == "app" and unit_id in APPS:
            APPS[unit_id].discard(ws)
            if not APPS[unit_id]:
                del APPS[unit_id]


def main() -> None:
    global SECRET
    ap = argparse.ArgumentParser(description="SENTRA reference relay server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--secret", default=SECRET)
    args = ap.parse_args()
    SECRET = args.secret
    async def _serve() -> None:
        async with websockets.serve(relay, "0.0.0.0", args.port):
            log.info("Relay server listening on :%d", args.port)
            await asyncio.Future()  # run until cancelled

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
