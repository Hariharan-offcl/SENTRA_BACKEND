import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from services import call_service

router = APIRouter(prefix="/api/v1/call", tags=["Video Call REST"])


@router.get("/ping")
def ping():
    now = time.time()
    return JSONResponse({
        "status": "ok",
        "transport": "websocket",
        "node_clients": len(call_service.call_clients["node"]),
        "user_clients": len(call_service.call_clients["user"]),
        "node_active": call_service.node_frame is not None and (now - call_service.node_last_seen) < 5,
        "user_active": call_service.user_frame is not None and (now - call_service.user_last_seen) < 5,
        "node_last_seen_ago_s": round(now - call_service.node_last_seen, 2) if call_service.node_last_seen else None,
        "user_last_seen_ago_s": round(now - call_service.user_last_seen, 2) if call_service.user_last_seen else None,
    })


@router.post("/upload-node")
async def upload_node(request: Request):
    try:
        raw = await request.body()
        if raw:
            await call_service.update_node_frame(raw)
    except ClientDisconnect:
        pass
    return {"status": "ok"}


@router.post("/upload-user")
async def upload_user(request: Request):
    try:
        raw = await request.body()
        if raw:
            await call_service.update_user_frame(raw)
    except ClientDisconnect:
        pass
    return {"status": "ok"}


@router.get("/node-stream.mjpg")
def node_stream():
    def get_frame():
        if call_service.node_frame and (time.time() - call_service.node_last_seen) < 5:
            return call_service.node_frame
        return call_service.generate_placeholder_frame("Awaiting Node (Phone A) feed...")

    return StreamingResponse(
        call_service.mjpeg_generator(get_frame),
        media_type=f"multipart/x-mixed-replace; boundary={call_service.MJPEG_BOUNDARY.decode()}",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/peer-stream.mjpg")
def peer_stream():
    def get_frame():
        if call_service.user_frame and (time.time() - call_service.user_last_seen) < 5:
            return call_service.user_frame
        return call_service.generate_placeholder_frame("Awaiting User (Phone B) feed...")

    return StreamingResponse(
        call_service.mjpeg_generator(get_frame),
        media_type=f"multipart/x-mixed-replace; boundary={call_service.MJPEG_BOUNDARY.decode()}",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
