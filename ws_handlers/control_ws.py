"""
WebSocket: /ws/control
Receives locomotion commands from the Flutter D-pad at 20 Hz.
Each received message is dispatched to the motor service immediately.
"""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import motor_service

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/control")
async def control_ws(websocket: WebSocket):
    """
    20 Hz motor control loop.
    Receives:
      { "linear_velocity": 0.6, "angular_velocity": 0.0, "direction": "FORWARD" }
    Sends back an ACK after each command.
    """
    await websocket.accept()
    logger.info("Control WS connected")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
                linear = float(payload.get("linear_velocity", 0.0))
                angular = float(payload.get("angular_velocity", 0.0))
                direction = payload.get("direction", "STOP")

                motor_service.apply_locomotion(linear, angular, direction)

                # Lightweight ACK so the app can detect dropped frames
                await websocket.send_text(
                    json.dumps({"ack": True, "direction": direction})
                )
            except (json.JSONDecodeError, KeyError, ValueError) as parse_err:
                logger.warning("Control WS bad payload: %s — %s", raw, parse_err)
                await websocket.send_text(json.dumps({"error": str(parse_err)}))

    except WebSocketDisconnect:
        logger.info("Control WS disconnected — halting motors")
        motor_service.apply_locomotion(0.0, 0.0, "STOP")
    except Exception as exc:
        logger.error("Control WS error: %s", exc)
        motor_service.apply_locomotion(0.0, 0.0, "STOP")
