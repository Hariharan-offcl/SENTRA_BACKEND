"""
SENTRA — Final app multiplexed WebSocket (Phase 21).

The single socket the final Flutter spec uses:

    ws://<pi-ip>:8080/ws?token=<jwt>

Envelope both directions (exact app-spec format):
    {"event": "...", "data": {...}}

Server → app push loops (per connected client):
    telemetry          1 Hz   (app spec §4)
    sensor_update      2 Hz   (app spec §6)
    apriltag_detected  on visible-set change (app spec §7)
    person_detected    on tracked-set change (app spec §11)
    navigation_status  1 Hz during autonomous movement (app spec §8/9)
    alert / emergency  instant, from the safety-event listener (app spec §12)

App → server handling:
    control_move / control_stop / control_estop   (§5 — motion controller)
    voice_command                                 (§10 — voice_service)
    camera_status                                 (§11/§17 — rover phone)
    ping → pong                                   (§15 keep-alive)

Token: ?token= JWT (Phase 15 verification); enforcement follows
SENTRA_AUTH_ENFORCED (when off, the socket accepts anonymous viewers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import auth as core_auth
from core import config as core_config
from services import compat_map as cm
from services import motor_service
from services.connection_manager import connection_manager

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)


async def send_event(ws: WebSocket, event: str, data: dict) -> None:
    await ws.send_text(json.dumps({"event": event, "data": data}))


def push_event(event: str, data: dict) -> None:
    """Thread-safe push of one event to every connected app client."""
    import asyncio as _aio
    loop = _aio.get_event_loop()

    # Only push to USER roles for the app compat socket
    users = connection_manager.get_connections_by_role("USER")
    for ws in list(users):
        try:
            _aio.run_coroutine_threadsafe(
                send_event(ws, event, data), loop)
        except RuntimeError:
            pass  # loop shutting down


# ── Safety events → alert / emergency (instant push) ─────────────────────────

def _on_safety_event(event: dict) -> None:
    try:
        from services import notification_service
        n = notification_service.from_safety_event(event)
        if n is None:
            return
        push_event("alert", cm.alert_object(n))
        if event.get("severity") == "DANGER":
            push_event("emergency", cm.emergency_object(n))
    except Exception as exc:
        logger.error("compat safety push failed: %s", exc)


# ── App → server handlers ────────────────────────────────────────────────────

def _handle_control_move(data: dict) -> None:
    from services.motion_controller import get_motion_controller
    mc = get_motion_controller()
    try:
        left = float(data.get("left_speed", 0.0)) * 100.0
        right = float(data.get("right_speed", 0.0)) * 100.0
    except (TypeError, ValueError):
        return
    mc.set_wheel_target(left, right, source="app_compat_ws", stream=True)


def _handle_voice_command(data: dict) -> None:
    from services import voice_service
    result = voice_service.execute(str(data.get("command", "")),
                                   target=(data.get("target") or None))
    if not result.get("handled") and result.get("error") == "missing_target":
        push_event("voice_error", result)
    logger.info("voice_command: %s", result)


def _handle_estop() -> None:
    result = motor_service.trigger_estop()
    logger.critical("E-STOP triggered via app compat WS")
    push_event("telemetry", cm.telemetry_event())
    return result


# ── Push loops ───────────────────────────────────────────────────────────────

async def _push_loop(ws: WebSocket) -> None:
    """All periodic pushes for one client in a single task."""
    last_sensors = time.time()
    last_tags_sig = None
    last_persons_sig = None
    last_nav = ""

    while True:
        await send_event(ws, "telemetry", cm.telemetry_event())

        now = time.time()
        if now - last_sensors >= 0.5:
            last_sensors = now
            await send_event(ws, "sensor_update", cm.sensor_event())

        # apriltag_detected — only on visible-set change
        from services import apriltag_service
        visible = apriltag_service.get_visible()
        sig = tuple(sorted((d["tag_id"], round(d.get("confidence") or 0, 2))
                           for d in visible))
        if sig != last_tags_sig:
            last_tags_sig = sig
            for det in visible:
                await send_event(ws, "apriltag_detected", cm.apriltag_event(det))

        # person_detected — only on tracked-set change
        from services import person_detection
        tracks = person_detection.get_tracked()
        psig = tuple(sorted((t["person_id"], round(t.get("confidence") or 0, 2))
                            for t in tracks))
        if psig != last_persons_sig:
            last_persons_sig = psig
            for tr in tracks:
                await send_event(ws, "person_detected", cm.person_event(tr))

        # navigation_status — during autonomy (throttled to 1 Hz)
        nav = cm.navigation_event()
        nav_sig = (nav["status"], nav.get("next_waypoint"))
        if nav["status"] != "idle" or nav_sig != last_nav:
            if nav["status"] != "idle" or now - getattr(_push_loop, "_last_nav_at", 0) >= 1.0:
                _push_loop._last_nav_at = now
                last_nav = nav_sig
                await send_event(ws, "navigation_status", nav)

        await asyncio.sleep(1.0)  # telemetry cadence = loop cadence


# ── Connection lifecycle ─────────────────────────────────────────────────────

async def _recv_loop(ws: WebSocket) -> None:
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event = str(msg.get("event", ""))
        data = msg.get("data") or {}

        if event == "ping":
            await send_event(ws, "pong", {})
        elif event == "control_move":
            _handle_control_move(data)
        elif event == "control_stop":
            from services.motion_controller import get_motion_controller
            get_motion_controller().stop("app_compat_ws")
        elif event == "control_estop":
            _handle_estop()
        elif event == "voice_command":
            _handle_voice_command(data)
        elif event == "camera_status":
            logger.info("Rover camera status: %s", data)
            push_event("camera_status", data)  # relay to other clients
        else:
            logger.debug("compat WS unknown event: %s", event)


@router.websocket("/ws")
async def compat_ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "").strip()
    ctx = None
    if core_auth.AUTH_ENFORCED:
        if token:
            try:
                ctx = core_auth.verify_token(token)
            except Exception as exc:
                await websocket.accept()
                await websocket.close(code=4401)
                logger.info("compat WS denied (bad token): %s", exc)
                return
        else:
            await websocket.accept()
            await websocket.close(code=4401)
            logger.info("compat WS denied (missing token)")
            return
    else:
        ctx = core_auth.AuthContext(device_id="anon", role="GUEST",
                                    permissions=[], kind="user", jti="-")

    await connection_manager.connect(websocket, role="USER", user_id=ctx.device_id)
    logger.info("App compat WS connected (%s) total=%d",
                ctx.device_id, len(connection_manager.active_connections))

    pusher = asyncio.create_task(_push_loop(websocket))
    try:
        await _recv_loop(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("compat WS error: %s", exc)
    finally:
        pusher.cancel()
        connection_manager.disconnect(websocket)
        # Safety: this client may have been driving.
        from services.motion_controller import get_motion_controller
        mc = get_motion_controller()
        mc.stop("app_compat_ws")
        motor_service.stop_all("app_compat_ws_disconnect")
        logger.info("App compat WS disconnected total=%d", len(connection_manager.active_connections))
