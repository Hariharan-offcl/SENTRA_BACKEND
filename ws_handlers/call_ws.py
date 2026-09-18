import json
import time
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import call_service

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)

@router.websocket("/ws/webrtc/{role}")
async def webrtc_signaling_socket(websocket: WebSocket, role: str):
    if role not in call_service.webrtc_clients:
        await websocket.close(code=1008, reason="role must be 'node' or 'user'")
        return

    await websocket.accept()
    previous_client = call_service.webrtc_clients[role]
    call_service.webrtc_clients[role] = websocket
    if previous_client is not None and previous_client is not websocket:
        try:
            await previous_client.close(
                code=1012,
                reason="Replaced by a newer connection for this role",
            )
        except Exception:
            pass
    peer_role = "user" if role == "node" else "node"
    allowed_types = {"ready", "offer", "answer", "candidate", "bye"}

    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            message_type = message.get("type")
            if message_type not in allowed_types:
                continue

            if message_type == "ready":
                if call_service.webrtc_clients[peer_role] is not None:
                    await websocket.send_json({"type": "peer-ready", "role": peer_role})
                await call_service.broadcast_signal(
                    peer_role,
                    {"type": "peer-ready", "role": role},
                )
                continue

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
        if call_service.webrtc_clients[role] is websocket:
            call_service.webrtc_clients[role] = None
            await call_service.broadcast_signal(peer_role, {"type": "peer-left", "role": role})


@router.websocket("/ws/call/{role}")
async def call_socket(websocket: WebSocket, role: str):
    if role not in call_service.call_clients:
        await websocket.close(code=1008, reason="role must be 'node' or 'user'")
        return

    await websocket.accept()
    call_service.call_clients[role].add(websocket)
    peer_role = "user" if role == "node" else "node"

    # Give a newly connected phone the freshest peer frame immediately.
    latest_peer_frame = call_service.user_frame if role == "node" else call_service.node_frame
    latest_peer_seen = call_service.user_last_seen if role == "node" else call_service.node_last_seen
    
    if (
        latest_peer_frame
        and time.time() - latest_peer_seen < 5
        and call_service.is_valid_jpeg(latest_peer_frame)
    ):
        await websocket.send_bytes(latest_peer_frame)

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            frame = message.get("bytes")
            if frame is None or not call_service.is_valid_jpeg(frame):
                continue

            if role == "node":
                call_service.node_frame = frame
                call_service.node_last_seen = time.time()
            else:
                call_service.user_frame = frame
                call_service.user_last_seen = time.time()

            await call_service.broadcast_frame(peer_role, frame)
            
    except WebSocketDisconnect:
        pass
    finally:
        call_service.call_clients[role].discard(websocket)
