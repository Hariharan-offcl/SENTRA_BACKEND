"""
SENTRA — Cloud relay client (Phase 16).

Gives a caregiver app remote access to this unit WITHOUT port forwarding:
the Pi dials OUT to a relay server (ws:// or wss://) and keeps a persistent
tunnel open. The app connects to the same relay, binds to this unit, and its
HTTP requests are proxied down the tunnel and executed LOCALLY against this
backend's ASGI app — auth stays end-to-end (the relay never sees valid
credentials it can reuse; Phase 15 JWTs are verified here, in the unit).

Design doc: docs/BACKEND_ARCHITECTURE.md §32.

Envelope protocol (JSON over the tunnel WebSocket):
    unit → relay : {"type":"unit_hello","unit_id":…,"secret":…}
    relay → unit : {"type":"hello_ok","unit_id":…} | {"type":"hello_error",…}
    unit → relay : {"type":"pong","ts":…}                      (heartbeat)
    app  → relay → unit : {"type":"http","id":…,"method":…,"path":…,
                            "query":{…},"headers":{…},"body_b64":…}
    unit → relay → app  : {"type":"http_response","id":…,"status":…,
                            "headers":{…},"body_b64":…,"truncated":bool}
    unit → relay → app  : {"type":"push","payload":{…}}        (spontaneous)

Only DANGER safety events are pushed spontaneously (FALL, PERSON_UNKNOWN,
CLIFF, ESTOP …) — the remote-app story needs the alarm, not the chatter.

Env vars:
    SENTRA_RELAY_URL           (default empty → relay disabled)
    SENTRA_RELAY_UNIT_SECRET   (default sentra-relay-dev-secret)
    SENTRA_RELAY_RECONNECT_MAX_S  (default 30)
    SENTRA_RELAY_BODY_MAX      (default 262144 — 256 KB response cap)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

RELAY_URL = os.getenv("SENTRA_RELAY_URL", "").strip()
UNIT_SECRET = os.getenv("SENTRA_RELAY_UNIT_SECRET", "sentra-relay-dev-secret")
RECONNECT_MAX_S = float(os.getenv("SENTRA_RELAY_RECONNECT_MAX_S", "30"))
BODY_MAX = int(os.getenv("SENTRA_RELAY_BODY_MAX", "262144"))
HEARTBEAT_S = 15.0
UNIT_ID = os.getenv("SENTRA_UNIT_ID", "SNT-9042")

_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_task: Optional[asyncio.Task] = None
_stop_event = threading.Event()
_status = {
    "enabled": bool(RELAY_URL),
    "connected": False,
    "unit_id": UNIT_ID,
    "url": RELAY_URL,
    "connected_since": None,
    "reconnects": 0,
    "last_rtt_ms": None,
    "last_error": None,
}


def configure_from_env() -> bool:
    """Re-read env config (call from lifespan before start())."""
    global RELAY_URL, UNIT_SECRET, UNIT_ID
    RELAY_URL = os.getenv("SENTRA_RELAY_URL", "").strip()
    UNIT_SECRET = os.getenv("SENTRA_RELAY_UNIT_SECRET", UNIT_SECRET)
    UNIT_ID = os.getenv("SENTRA_UNIT_ID", UNIT_ID)
    with _lock:
        _status["enabled"] = bool(RELAY_URL)
        _status["url"] = RELAY_URL
        _status["unit_id"] = UNIT_ID
    return bool(RELAY_URL)


def attach_loop(loop) -> None:
    global _loop
    _loop = loop


def start() -> None:
    """Launch the tunnel task (no-op when disabled or already running)."""
    global _task
    if not RELAY_URL:
        logger.info("Relay client disabled (no SENTRA_RELAY_URL)")
        return
    if _task is not None and not _task.done():
        return
    _stop_event.clear()
    if _loop is None:
        logger.warning("Relay client cannot start: no event loop attached")
        return
    _task = _loop.create_task(_run())
    logger.info("Relay client starting → %s", RELAY_URL)


def stop() -> None:
    _stop_event.set()


def get_status() -> dict:
    with _lock:
        s = dict(_status)
    if s["connected_since"]:
        s["uptime_s"] = round(time.time() - s["connected_since"], 1)
    return s


def send_push(payload: dict) -> None:
    """Fire-and-forget push to the relay (any thread). Dropped when offline."""
    loop, task = _loop, _task
    if loop is None or task is None or task.done():
        return
    asyncio.run_coroutine_threadsafe(_push_coro(payload), loop)


def _on_safety_event(event: dict) -> None:
    """safety_events listener: forward DANGER events over the tunnel."""
    if event.get("severity") == "DANGER":
        logger.info("Relay push: forwarding %s event to relay", event.get("type"))
        send_push({"kind": "safety_event", "event": event.get("type"),
                   "severity": event.get("severity"), "detail": event.get("detail"),
                   "timestamp": event.get("timestamp")})


async def _push_coro(payload: dict) -> None:
    ws = _live_ws
    if ws is None:
        logger.warning("Relay push dropped: tunnel not up")
        return
    try:
        await ws.send(json.dumps({"type": "push", "payload": payload}))
        logger.info("Relay push sent: %s", payload.get("event"))
    except Exception as exc:
        logger.warning("Relay push failed: %s", exc)


# ── Local ASGI dispatch (proxied REST executes against this backend) ─────────

_HDR_WHITELIST = {"authorization", "content-type", "accept"}


async def dispatch_local(method: str, path: str, query: dict | None,
                         headers: dict | None, body: bytes) -> dict:
    """Run one HTTP request through this unit's own ASGI app (middleware
    included, so tunnel traffic is authenticated exactly like LAN traffic)."""
    from main import app  # deferred: avoid import cycle at module load
    pairs = [(k.lower(), v) for k, v in (headers or {}).items()
             if k.lower() in _HDR_WHITELIST]
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method.upper(), "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": urllib.parse.urlencode(query or {}).encode(),
        "headers": [(k.encode(), v.encode()) for k, v in pairs],
        "client": ("relay-tunnel", 0), "server": ("local", 0),
    }
    out = {"status": 500, "headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = [(k.decode(), v.decode())
                              for k, v in msg.get("headers", [])]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    try:
        await app(scope, receive, send)
    except Exception as exc:
        logger.error("Relay dispatch failed: %s", exc)
        out = {"status": 500, "headers": [], "body": json.dumps(
            {"detail": "relay dispatch error"}).encode()}
    truncated = len(out["body"]) > BODY_MAX
    return {
        "status": out["status"],
        "headers": dict(out["headers"]),
        "body_b64": base64.b64encode(out["body"][:BODY_MAX]).decode(),
        "truncated": truncated,
    }


# ── Tunnel loop ──────────────────────────────────────────────────────────────

async def _run() -> None:
    import websockets
    backoff = 1.0
    while not _stop_event.is_set():
        try:
            async with websockets.connect(RELAY_URL, open_timeout=10) as ws:
                await _session(ws)
                backoff = 1.0
        except asyncio.CancelledError:
            break
        except Exception as exc:
            with _lock:
                _status["connected"] = False
                _status["last_error"] = f"{type(exc).__name__}: {exc}"
            if _stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)
            with _lock:
                _status["reconnects"] += 1
    logger.info("Relay client stopped")


async def _session(ws) -> None:
    global _live_ws
    await ws.send(json.dumps({"type": "unit_hello",
                              "unit_id": UNIT_ID, "secret": UNIT_SECRET}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if reply.get("type") != "hello_ok":
        raise PermissionError(f"relay rejected unit: {reply}")
    logger.info("Relay link established (unit=%s)", UNIT_ID)
    with _lock:
        _status["connected"] = True
        _status["connected_since"] = time.time()
        _status["last_error"] = None
    _live_ws = ws
    heartbeat = asyncio.create_task(_heartbeat(ws))
    try:
        async for raw in ws:
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            etype = envelope.get("type")
            if etype == "http":
                asyncio.create_task(_handle_http(ws, envelope))
            elif etype == "ping":
                await ws.send(json.dumps({"type": "pong",
                                          "ts": envelope.get("ts")}))
            elif etype == "pong":
                ts = envelope.get("ts")
                if isinstance(ts, (int, float)):
                    with _lock:
                        _status["last_rtt_ms"] = round((time.time() - ts) * 1000, 1)
    finally:
        heartbeat.cancel()
        _live_ws = None
        with _lock:
            _status["connected"] = False


async def _heartbeat(ws) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        await ws.send(json.dumps({"type": "ping", "ts": time.time()}))


async def _handle_http(ws, envelope: dict) -> None:
    req_id = envelope.get("id", "")
    try:
        body = base64.b64decode(envelope.get("body_b64") or "")
        result = await dispatch_local(
            envelope.get("method", "GET"), envelope.get("path", "/"),
            envelope.get("query"), envelope.get("headers"), body)
    except Exception as exc:
        logger.error("Relay http envelope %s failed: %s", req_id, exc)
        result = {"status": 500, "headers": {},
                  "body_b64": base64.b64encode(
                      json.dumps({"detail": "unit error"}).encode()).decode(),
                  "truncated": False}
    await ws.send(json.dumps({"type": "http_response", "id": req_id, **result}))


_live_ws = None  # live tunnel socket while a session is up (set by _session)
