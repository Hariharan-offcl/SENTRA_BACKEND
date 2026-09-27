"""
WebSocket: /ws/control  (Phase 2)

Receives manual driving commands and feeds the motion controller (ramping,
timeouts) which pushes smoothed duty through the safety layer.

Accepted message forms (all JSON):
    Legacy (Flutter v1 — unchanged):
        {"linear_velocity": 0.6, "angular_velocity": 0.0, "direction": "FORWARD"}
        {"linear_velocity": 0.0, "angular_velocity": 0.0, "direction": "STOP"}
    New (Phase 2):
        {"type": "wheel",      "left": 50, "right": 50}
        {"type": "direction",  "direction": "FORWARD", "scale": 0.5}
        {"type": "stop"}
        {"type": "brake"}
        {"left_speed": 50, "right_speed": 50}          (convenience form)

Server pushes:
    ACK per command (legacy shape preserved: {"ack": true, "direction": ...})
    {"type": "state", ...motion state...} every 0.5 s (independent task —
    arrives even with no commands flowing, e.g. right after an e-stop)

On disconnect the rover is stopped.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.motion_controller import get_motion_controller
from services import motor_service
from core import auth as core_auth  # Phase 15: token gate on this socket

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)

_STATE_PUSH_INTERVAL_S = 0.5


async def _state_pusher(websocket: WebSocket, mc) -> None:
    """Push motion state to the client every 0.5 s, independent of commands."""
    try:
        while True:
            state = mc.get_state()
            await websocket.send_text(json.dumps({"type": "state", **state}))
            await asyncio.sleep(_STATE_PUSH_INTERVAL_S)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass  # socket closed — main handler cleans up


@router.websocket("/ws/control")
async def control_ws(websocket: WebSocket):
    """
    Manual control loop (20 Hz streaming recommended).
    Commands go through the motion controller → safety layer → motors.
    """
    ctx = await core_auth.enforce_ws_control(websocket)
    if ctx is None:
        return  # denied — enforce_ws_control already closed the socket
    await websocket.accept()
    mc = get_motion_controller()
    logger.info("Control WS connected")
    pusher = asyncio.create_task(_state_pusher(websocket, mc))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
                result = mc.handle_message(payload)
                await websocket.send_text(json.dumps(result))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as parse_err:
                logger.warning("Control WS bad payload: %s — %s", raw, parse_err)
                await websocket.send_text(json.dumps({"error": str(parse_err)}))

    except WebSocketDisconnect:
        logger.info("Control WS disconnected — halting motors")
        mc.stop("control_ws")
        motor_service.stop_all("control_ws_disconnect")
    except Exception as exc:
        logger.error("Control WS error: %s", exc)
        mc.stop("control_ws")
        motor_service.stop_all("control_ws_error")
    finally:
        pusher.cancel()
