"""
WebSocket: /ws/telemetry
Pushes a telemetry payload to every connected client at 10 Hz (100 ms interval).
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import telemetry_service

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)

# Connection pool — multiple dashboards can subscribe simultaneously
_clients: Set[WebSocket] = set()


@router.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
    """
    10 Hz real-time telemetry stream.
    The Flutter Dashboard connects here immediately after pairing.
    """
    await websocket.accept()
    _clients.add(websocket)
    logger.info("Telemetry WS connected  total=%d", len(_clients))

    try:
        while True:
            payload = telemetry_service.get_ws_telemetry_payload()
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(0.1)  # 10 Hz
    except WebSocketDisconnect:
        logger.info("Telemetry WS disconnected")
    except Exception as exc:
        logger.error("Telemetry WS error: %s", exc)
    finally:
        _clients.discard(websocket)
