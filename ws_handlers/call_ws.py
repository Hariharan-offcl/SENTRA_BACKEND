import time
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import call_service

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)


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
