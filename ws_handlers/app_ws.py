"""
SENTRA — Final app WebSocket channels (Phase 1 contract).

The two sockets the Flutter app actually dials (BackendConfig.webSocketUrl):

    ws://<pi>:8080/ws/user?token=<jwt>    — the human's phone      (role USER)
    ws://<pi>:8080/ws/rover?token=<jwt>   — the mounted phone      (role ROVER)

Inbound envelope: FLAT  {"type": "...", ...fields}   (wire.dart, useLegacyEnvelope=false)
Outbound envelope: {"event": "...", "data": {...}}   (ws_event.dart accepts both)

Inbound message catalog (SENTRA_API_CONTRACT.md §3):
    ping, motor_command, motor_stop, emergency_stop, emergency_reset,
    voice_command, command {STANDBY|START_PATROL|PAUSE_PATROL|RESUME_PATROL|STOP_PATROL},
    heartbeat, resync, announce,
    camera_start, camera_stop, camera_frame (ROVER channel only), camera_status

camera_frame is the ONLY frame ingress from the mounted phone: the base64 JPEG
is decoded and fed into call_service.update_node_frame, which re-encodes,
updates the latest-frame buffer and drives the vision hub (AprilTag, person
detection, fall detection) exactly like the /ws/call/node path.

Auth: ?token=<jwt> when SENTRA_AUTH_ENFORCED=true (close 4401 on failure).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import auth as core_auth
from services import compat_map as cm
from services import motor_service
from services.connection_manager import connection_manager
from ws_handlers.compat_ws import send_event, push_event

router = APIRouter(tags=["WebSocket App"])
logger = logging.getLogger(__name__)

# Liveness / state of the mounted rover phone (heartbeat + camera events).
_rover_state: dict = {
    "last_heartbeat": 0.0,
    "last_frame": 0.0,
    "camera_state": "INACTIVE",
    "battery_percent": None,
    "mode": None,
}
_last_frame_sequence = -1


def _flat_inbound(raw: str) -> tuple[str, dict]:
    """Parse an inbound message; returns (type, fields).

    Accepts the app's flat shape and, defensively, the legacy
    {"event","data"} shape (identical unwrap to ws_event.dart).
    """
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        return "", {}
    if "type" in msg:
        return str(msg["type"]), {k: v for k, v in msg.items() if k != "type"}
    if "event" in msg:
        return str(msg["event"]), dict(msg.get("data") or {})
    return "", {}


# ── Handlers (flat catalog) ──────────────────────────────────────────────────

def _mc():
    from services.motion_controller import get_motion_controller
    return get_motion_controller()


async def _handle_motor(ws: WebSocket, mtype: str, data: dict) -> None:
    """motor_command (−100..100 % duty) / motor_stop / estop / reset."""
    if mtype == "motor_command":
        try:
            left = max(-100.0, min(100.0, float(data.get("left", 0))))
            right = max(-100.0, min(100.0, float(data.get("right", 0))))
        except (TypeError, ValueError):
            await send_event(ws, "error", {"code": "bad_request",
                                           "detail": "left/right must be numbers"})
            return
        decision = _mc().set_wheel_target(left, right, source="app_ws", stream=True)
        await send_event(ws, "command_ack", {"command": mtype,
                                             "ok": bool(decision.get("applied"))})
    elif mtype == "motor_stop":
        _mc().stop("app_ws")
        await send_event(ws, "command_ack", {"command": mtype, "ok": True})
    elif mtype == "emergency_stop":
        result = motor_service.trigger_estop()
        logger.critical("E-STOP via app WS")
        await send_event(ws, "command_ack", {"command": mtype, "ok": True,
                                             "estop_active": True})
        push_event("telemetry", cm.telemetry_event())
    elif mtype == "emergency_reset":
        result = motor_service.reset_estop()
        await send_event(ws, "command_ack", {"command": mtype,
                                             "ok": not result.get("estop_active", True)})


async def _handle_voice(ws: WebSocket, data: dict) -> None:
    """voice_command with the Flutter vocabulary bridged (contract §5)."""
    from services import voice_service
    command = str(data.get("command", "")).strip()
    location = data.get("location") or data.get("target")
    transcript = data.get("transcript")

    upper = command.upper()
    if upper == "EMERGENCY_STOP":
        motor_service.trigger_estop()
        logger.critical("E-STOP via app WS voice_command")
        await send_event(ws, "command_ack", {"command": command, "ok": True})
        return
    if upper == "READ_STATUS":
        push_event("telemetry", cm.telemetry_event())
        await send_event(ws, "command_ack", {"command": command, "ok": True,
                                             "action": "read_status"})
        return
    if upper == "CALL_USER":
        result = _create_call()
        await send_event(ws, "command_ack", {"command": command, "ok": True,
                                             "call": result})
        return

    # Everything else: voice_service has the SHOUTED→legacy alias map.
    result = voice_service.execute(command, location=location, raw=transcript)
    await send_event(ws, "command_ack", {"command": command,
                                         "ok": bool(result.get("handled")),
                                         **{k: v for k, v in result.items()
                                            if k in ("action", "target", "error",
                                                     "state", "tag_id", "route")}})
    if not result.get("handled") and result.get("error"):
        logger.info("voice_command %r not handled: %s", command, result["error"])


async def _handle_command(ws: WebSocket, data: dict) -> None:
    """High-level command {STANDBY | START/PAUSE/RESUME/STOP_PATROL}."""
    from services import patrol_service, docking_service, tag_map
    command = str(data.get("command", "")).strip().upper()
    ok = False
    detail: dict = {}
    if command == "STANDBY":
        result = docking_service.start_return(docked_by="app_command")
        ok = bool(result.get("ok"))
        detail = {"action": "standby"}
    elif command == "START_PATROL":
        route_name = None
        ids = data.get("location_ids") or []
        if ids:
            names = []
            for loc in ids:
                s = str(loc)
                tag = None
                if s.startswith("loc-"):
                    try:
                        tag = tag_map.get_tag(int(s[4:]))
                    except ValueError:
                        tag = None
                tag = tag or tag_map.find_by_name(s)
                if tag is None:
                    await send_event(ws, "error", {"code": "unknown_location",
                                                   "detail": str(loc)})
                    return
                names.append(tag["name"])
            saved = patrol_service.save_route("app-patrol", names)
            if saved.get("ok"):
                route_name = "app-patrol"
        result = patrol_service.start_patrol(route_name)
        ok = bool(result.get("ok"))
        detail = {"action": "start_patrol", "route": route_name}
    elif command == "PAUSE_PATROL":
        # Phase 5 (P10): real pause — session kept, motors stopped.
        result = patrol_service.pause_patrol()
        ok = bool(result.get("ok"))
        detail = {"action": "pause_patrol"}
    elif command == "RESUME_PATROL":
        # Phase 5 (P10): resume from the paused waypoint.
        result = patrol_service.resume_patrol()
        ok = bool(result.get("ok"))
        detail = {"action": "resume_patrol"}
    elif command == "STOP_PATROL":
        result = patrol_service.stop_patrol()
        ok = bool(result.get("ok"))
        detail = {"action": "stop_patrol"}
    else:
        await send_event(ws, "error", {"code": "unknown_command", "detail": command})
        return
    if not ok and result.get("error"):
        detail["error"] = result["error"]
    await send_event(ws, "command_ack", {"command": command, "ok": ok, **detail})


async def _handle_heartbeat(ws: WebSocket, data: dict, is_rover: bool) -> None:
    if is_rover:
        _rover_state.update({
            "last_heartbeat": time.time(),
            "mode": data.get("mode"),
            "camera_state": data.get("camera_state"),
            "battery_percent": data.get("battery_percent"),
        })
    await send_event(ws, "heartbeat_ack", {})


async def _handle_resync(ws: WebSocket) -> None:
    """Re-push the full state so a reconnected phone never shows stale data."""
    await send_event(ws, "telemetry", cm.telemetry_event())
    await send_event(ws, "sensor_update", cm.sensor_event())
    await send_event(ws, "navigation_status", cm.navigation_event())


async def _handle_camera_frame(ws: WebSocket, data: dict) -> None:
    """Rover phone frame ingress → call_service buffer → vision hub."""
    from services import call_service
    if data.get("format", "jpeg") != "jpeg" or data.get("encoding", "base64") != "base64":
        await send_event(ws, "error", {"code": "unsupported_frame_format"})
        return
    try:
        jpeg = base64.b64decode(str(data.get("data", "")), validate=True)
    except Exception:
        await send_event(ws, "error", {"code": "bad_frame_encoding"})
        return
    if not jpeg.startswith(b"\xff\xd8"):
        await send_event(ws, "error", {"code": "not_a_jpeg"})
        return
    seq = data.get("sequence")
    try:
        seq = int(seq)
        if seq <= _last_frame_sequence:
            logger.debug("camera_frame sequence regression %s → %s",
                         _last_frame_sequence, seq)
        globals()["_last_frame_sequence"] = seq
    except (TypeError, ValueError):
        pass
    _rover_state["last_frame"] = time.time()
    _rover_state["camera_state"] = "ACTIVE"
    # Re-encode + latest-frame buffer + broadcast to call peers + vision sampler.
    await call_service.update_node_frame(jpeg)


# ── Shared push loop (per connection) ────────────────────────────────────────

async def _push_loop(ws: WebSocket) -> None:
    last_sensors = 0.0
    last_nav_sig = None
    last_tags_sig = None
    last_persons_sig = None
    from services import telemetry_service
    while True:
        await send_event(ws, "telemetry", telemetry_service.get_ws_telemetry_payload())
        now = time.time()
        if now - last_sensors >= 0.5:
            last_sensors = now
            await send_event(ws, "sensor_update", cm.sensor_event())

        # apriltag_detected — only on visible-set change (same rule as the
        # legacy /ws loop; without this the app's vision UI stayed silent).
        from services import apriltag_service
        visible = apriltag_service.get_visible()
        tag_sig = tuple(sorted((d["tag_id"], round(d.get("confidence") or 0, 2))
                               for d in visible))
        if tag_sig != last_tags_sig:
            last_tags_sig = tag_sig
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

        nav = cm.navigation_event()
        sig = (nav.get("status"), nav.get("next_waypoint"))
        if sig != last_nav_sig or nav.get("status") not in (None, "idle"):
            last_nav_sig = sig
            await send_event(ws, "navigation_status", nav)
        await asyncio.sleep(1.0)


# ── Call helper (shared with the voice CALL_USER path) ───────────────────────

def _create_call() -> dict:
    """Create a ringing call and push call_incoming (contract §6 calls)."""
    import uuid
    call = {
        "id": f"call-{uuid.uuid4().hex[:8]}",
        "robot_id": cm.ROBOT_ID,
        "status": "ringing",
        "started_at": time.time(),
    }
    from routers import compat
    compat.register_call(call)  # stores + pushes call_incoming
    return {"id": call["id"], "status": call["status"]}


# ── Connection lifecycle ─────────────────────────────────────────────────────

async def _app_socket(ws: WebSocket, is_rover: bool) -> None:
    channel = "/ws/rover" if is_rover else "/ws/user"
    token = ws.query_params.get("token", "").strip()
    if core_auth.AUTH_ENFORCED:
        if not token:
            await ws.accept()
            await ws.close(code=4401)
            logger.info("%s denied (missing token)", channel)
            return
        try:
            ctx = core_auth.verify_token(token)
        except Exception as exc:
            await ws.accept()
            await ws.close(code=4401)
            logger.info("%s denied (bad token): %s", channel, exc)
            return
        device_id = ctx.device_id
    else:
        device_id = token or f"anon-{id(ws)}"

    role = "ROVER" if is_rover else "USER"
    # connect() performs the websocket.accept() itself — do NOT accept twice.
    await connection_manager.connect(ws, role=role, user_id=device_id)
    logger.info("%s connected (%s) role=%s", channel, device_id, role)
    if is_rover:
        _rover_state["last_heartbeat"] = time.time()

    pusher = asyncio.create_task(_push_loop(ws))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                mtype, data = _flat_inbound(raw)
            except json.JSONDecodeError:
                await send_event(ws, "error", {"code": "bad_json"})
                continue
            if not mtype:
                await send_event(ws, "error", {"code": "unknown_type"})
                continue

            if mtype == "ping":
                await send_event(ws, "pong", {})
            elif mtype in ("motor_command", "motor_stop",
                           "emergency_stop", "emergency_reset"):
                await _handle_motor(ws, mtype, data)
            elif mtype == "voice_command":
                await _handle_voice(ws, data)
            elif mtype == "command":
                await _handle_command(ws, data)
            elif mtype == "heartbeat":
                await _handle_heartbeat(ws, data, is_rover)
            elif mtype == "resync":
                await _handle_resync(ws)
            elif mtype == "announce":
                logger.info("%s announce: %s", channel, data)
            elif mtype == "camera_start":
                _rover_state["camera_state"] = "ACTIVE"
                logger.info("rover camera start: %sx%s @%sfps q%s",
                            data.get("width"), data.get("height"),
                            data.get("fps"), data.get("jpeg_quality"))
                push_event("camera_status", {"is_streaming": True})
            elif mtype == "camera_stop":
                _rover_state["camera_state"] = "INACTIVE"
                push_event("camera_status", {"is_streaming": False})
            elif mtype == "camera_status":
                push_event("camera_status", data)
            elif mtype == "camera_frame":
                if not is_rover:
                    await send_event(ws, "error",
                                     {"code": "camera_frame_forbidden",
                                      "detail": "frames are accepted on /ws/rover only"})
                else:
                    await _handle_camera_frame(ws, data)
            else:
                logger.debug("%s unknown type: %s", channel, mtype)
                await send_event(ws, "error", {"code": "unknown_type",
                                               "type": mtype})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("%s error: %s", channel, exc)
    finally:
        pusher.cancel()
        connection_manager.disconnect(ws)
        logger.info("%s disconnected (%s)", channel, device_id)


@router.websocket("/ws/user")
async def app_user_socket(websocket: WebSocket):
    await _app_socket(websocket, is_rover=False)


@router.websocket("/ws/rover")
async def app_rover_socket(websocket: WebSocket):
    await _app_socket(websocket, is_rover=True)
