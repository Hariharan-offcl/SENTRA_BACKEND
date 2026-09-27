"""
Router: Camera REST endpoints
  GET  /api/v1/camera/stream.mjpg   — MJPEG stream
  POST /api/v1/camera/ir-filter     — Toggle IR Night Vision relay
  POST /api/v1/camera/snapshot      — Capture high-res snapshot
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config import settings
from models.requests import IRFilterRequest
from models.responses import IRFilterResponse, SnapshotResponse
from services import camera_service

router = APIRouter(prefix="/api/v1/camera", tags=["Camera"])
logger = logging.getLogger(__name__)


@router.get("/stream.mjpg")
def video_stream():
    """
    MJPEG over HTTP — consumed directly by the Flutter VideoPlayer widget.
    Boundary: frame | Content-Type: multipart/x-mixed-replace
    """
    return StreamingResponse(
        camera_service.mjpeg_frame_generator(quality=settings.mjpeg_quality),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.post("/ir-filter", response_model=IRFilterResponse)
def toggle_ir_filter(body: IRFilterRequest):
    """Toggle the hardware IR Night Vision relay (GPIO relay on Pi)."""
    result = camera_service.toggle_ir_filter(body.enabled)
    return IRFilterResponse(**result)


@router.post("/snapshot", response_model=SnapshotResponse)
def capture_snapshot(request: Request):
    """
    Capture a full-resolution JPEG and save it to the snapshot directory.
    Returns the local file path and a download URL.
    """
    host = request.headers.get("host", f"127.0.0.1:{settings.port}").split(":")[0]
    result = camera_service.capture_snapshot(
        snapshot_dir=settings.camera_snapshot_dir,
        host_ip=host,
        port=settings.port,
    )
    return SnapshotResponse(**result)
