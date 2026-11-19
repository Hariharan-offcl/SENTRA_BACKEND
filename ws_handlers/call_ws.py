import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import call_service, emergency_call
from services.connection_manager import connection_manager

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)

# Track whether the user has sent 'ready' (and rover wasn't yet connected).
# When rover connects, we re-send peer-ready so it starts the offer.
_user_ready_since: float = 0.0   # 0 = not pending; epoch time otherwise
_USER_READY_TTL_S = 30.0          # discard stale 'ready' after 30 s


@router.websocket("/ws/webrtc/{role}")
async def webrtc_signaling_socket(websocket: WebSocket, role: str):
    global _user_ready_since
    # Mapping internal role terminology to ConnectionManager roles
    cm_role = "ROVER" if role == "node" else "USER"
    if role not in call_service.webrtc_clients:
        await websocket.close(code=1008, reason="role must be 'node' or 'user'")
        return
    # Phase 4: token gate (?token=) - 4401 when enforced and missing/bad.
    from core import auth as core_auth
    if await core_auth.verify_ws_token(websocket) is None:
        return

    await connection_manager.connect(websocket, role=cm_role, user_id=role)

    previous_client = call_service.webrtc_clients[role]
    call_service.webrtc_clients[role] = websocket
    emergency_call.note_presence(role, True)
    if previous_client is not None and previous_client is not websocket:
        try:
            await previous_client.close(
                code=1012,
                reason="Replaced by a newer connection for this role",
            )
        except Exception:
            pass

    # Race-condition fix: if rover just connected and user had already sent
    # 'ready' (but rover wasn't registered yet), relay peer-ready now.
    if role == "node":
        age = time.time() - _user_ready_since
        if 0 < age < _USER_READY_TTL_S:
            logger.info("Rover connected after user ready -- relaying peer-ready to rover")
            try:
                await websocket.send_json({"type": "peer-ready", "role": "user"})
            except Exception:
                pass

    peer_role = "user" if role == "node" else "node"
    allowed_types = {"ready", "offer", "answer", "candidate", "bye", "ping"}

    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            message_type = message.get("type")
            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if message_type not in allowed_types:
                continue

            if message_type == "ready":
                peer = call_service.webrtc_clients[peer_role]
                if peer is not None:
                    # Peer is online - normal flow: tell sender their peer is ready
                    # and tell peer that this side is ready (triggering offer from rover).
                    await websocket.send_json({"type": "peer-ready", "role": peer_role})
                    await call_service.broadcast_signal(
                        peer_role,
                        {"type": "peer-ready", "role": role},
                    )
                    if role == "user":
                        _user_ready_since = 0.0  # rover is online, no need to cache
                else:
                    # Peer not yet connected - remember that user is ready.
                    if role == "user":
                        _user_ready_since = time.time()
                        logger.info("User sent 'ready' but rover not connected yet -- cached")
                continue

            if message_type == "bye" and role == "user":
                _user_ready_since = 0.0
            forwarded = {"type": message_type, "from": role}
            if message_type in {"offer", "answer"}:
                sdp = message.get("sdp")
                if not isinstance(sdp, str) or len(sdp) > 1_000_000:
                    continue
                forwarded["sdp"] = sdp
            elif message_type == "candidate":
                candidate = message.get("candidate")
                if not isinstance(candidate, str) or len(candidate) > 16_384:
                    continue
                forwarded.update({
                    "candidate": candidate,
                    "sdpMid": message.get("sdpMid"),
                    "sdpMLineIndex": message.get("sdpMLineIndex"),
                })

            await call_service.broadcast_signal(peer_role, forwarded)
    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(websocket)
        emergency_call.note_presence(role, False)
        if call_service.webrtc_clients[role] is websocket:
            call_service.webrtc_clients[role] = None
            await call_service.broadcast_signal(peer_role, {"type": "peer-left", "role": role})
        # Clear the ready-cache if user disconnects
        if role == "user":
            _user_ready_since = 0.0

@router.websocket("/ws/call/{role}")
async def call_socket(websocket: WebSocket, role: str):
    cm_role = "ROVER" if role == "node" else "USER"
    if role not in call_service.call_clients:
        await websocket.close(code=1008, reason="role must be 'node' or 'user'")
        return
    # Phase 4: token gate (?token=) - 4401 when enforced and missing/bad.
    from core import auth as core_auth
    if await core_auth.verify_ws_token(websocket) is None:
        return

    await connection_manager.connect(websocket, role=cm_role, user_id=role)
    pull = websocket.query_params.get("pull") == "1"
    if not pull:
        call_service.call_clients[role].add(websocket)
    peer_role = "user" if role == "node" else "node"
    last_sent = None

    async def send_latest(*, reply: bool = False):
        nonlocal last_sent
        frame, seen = ((call_service.user_frame, call_service.user_last_seen)
                       if role == "node" else
                       (call_service.node_frame, call_service.node_last_seen))
        if (seen != last_sent and time.time() - seen < 2
                and frame and call_service.is_valid_jpeg(frame)):
            last_sent = seen
            await websocket.send_bytes(frame)
        elif reply:
            await websocket.send_bytes(b"")

    try:
        if pull:
            await websocket.send_text("relay_pull")
        else:
            await send_latest()
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if pull and message.get("text") == "latest":
                await send_latest(reply=True)
                continue

            frame = message.get("bytes")
            if frame is None:
                continue
            if not call_service.is_valid_jpeg(frame):
                if pull:
                    await websocket.send_text("uploaded")
                continue

            if role == "node":
                call_service.node_frame = frame
                call_service.node_last_seen = time.time()
            else:
                call_service.user_frame = frame
                call_service.user_last_seen = time.time()

            if pull:
                await websocket.send_text("uploaded")
            await call_service.broadcast_frame(peer_role, frame)

    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(websocket)
        call_service.call_clients[role].discard(websocket)
