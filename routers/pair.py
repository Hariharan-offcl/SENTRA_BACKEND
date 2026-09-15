"""
Router: Pairing endpoint
  POST /api/v1/pair
"""

import logging

from fastapi import APIRouter, Request

from config import settings
from models.requests import PairRequest
from models.responses import PairResponse

router = APIRouter(prefix="/api/v1", tags=["Pairing"])
logger = logging.getLogger(__name__)

# Active pair registry (in-memory; extend to Redis/SQLite if needed)
_paired_apps: dict[str, str] = {}


@router.post("/pair", response_model=PairResponse)
def pair_device(body: PairRequest, request: Request):
    """
    Register a mobile app instance with this Pi unit.
    Returns WebSocket and stream URLs so the app can connect.
    """
    _paired_apps[body.app_instance_id] = body.ip_address
    logger.info("Device paired  app=%s  ip=%s", body.app_instance_id, body.ip_address)

    pi_ip = body.ip_address  # The IP the app knows this Pi by
    port = settings.port

    return PairResponse(
        paired=True,
        unit_id=settings.unit_id,
        unit_name=settings.unit_name,
        websocket_telemetry_url=f"ws://{pi_ip}:{port}/ws/telemetry",
        websocket_control_url=f"ws://{pi_ip}:{port}/ws/control",
        stream_url=f"http://{pi_ip}:{port}/api/v1/camera/stream.mjpg",
    )
