import time
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from services import call_service

router = APIRouter(prefix="/api/v1/call", tags=["Video Call"])


@router.get("/ping")
def ping_call_status():
    return {
        "status": "online",
        "unit_id": "SENTRA-PI5",
        "server": "FastAPI/Python3",
        "timestamp": int(time.time()),
        "node_active": (time.time() - call_service.node_last_seen) < 5.0,
        "user_active": (time.time() - call_service.user_last_seen) < 5.0
    }


@router.post("/upload-node")
async def upload_node_frame(request: Request):
    """Receive a raw JPEG frame from Phone A (Node)"""
    body = await request.body()
    if body:
        call_service.update_node_frame(body)
    return {"status": "ok"}


@router.post("/upload-user")
async def upload_user_frame(request: Request):
    """Receive a raw JPEG frame from Phone B (User)"""
    body = await request.body()
    if body:
        call_service.update_user_frame(body)
    return {"status": "ok"}


@router.get("/node-stream.mjpg")
def node_stream():
    """Stream frames sent by Phone A to Phone B"""
    return StreamingResponse(
        call_service.stream_generator("node"),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@router.get("/peer-stream.mjpg")
def peer_stream():
    """Stream frames sent by Phone B to Phone A"""
    return StreamingResponse(
        call_service.stream_generator("user"),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )
