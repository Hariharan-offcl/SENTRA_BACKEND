"""
Router: Manual control REST endpoints (Phase 2)
  POST /api/v1/control/move         — one-shot wheel/directional move
  POST /api/v1/control/stop         — ramp to stop
  POST /api/v1/control/brake        — stop + active brake pulse
  POST /api/v1/control/estop        — emergency stop (same latch as /robot/estop)
  POST /api/v1/control/estop/reset  — clear e-stop latch
  GET  /api/v1/control/state        — live motor/motion state

Notes for the app:
  - /move without duration applies a brief 1.0 s move (one-shot).
  - Streaming/joystick control should use /ws/control, not this endpoint.
  - E-stop here is the SAME latch as /api/v1/robot/estop (one source of truth).
"""

import logging

from fastapi import APIRouter

from models.control import (
    MoveRequest,
    StopRequest,
    BrakeRequest,
    ControlActionResponse,
    ControlStateResponse,
)
from services.motion_controller import get_motion_controller
from services import motor_service

router = APIRouter(prefix="/api/v1/control", tags=["Manual Control"])
logger = logging.getLogger(__name__)

_BRIEF_MOVE_S = 1.0  # default one-shot duration when duration omitted


@router.post("/move", response_model=ControlActionResponse)
def control_move(body: MoveRequest):
    """One-shot manual move. Prefer /ws/control for continuous driving."""
    mc = get_motion_controller()

    if body.left_speed is not None and body.right_speed is not None:
        result = mc.set_wheel_target(body.left_speed, body.right_speed,
                                     source="rest_move", stream=False,
                                     duration=body.duration or _BRIEF_MOVE_S)
        return ControlActionResponse(action="move", **result)

    if body.direction is not None:
        result = mc.set_directional(body.direction, body.scale, source="rest_move",
                                    stream=False,
                                    duration=body.duration or _BRIEF_MOVE_S)
        return ControlActionResponse(action="move", **result)

    return ControlActionResponse(action="move", applied=False,
                                 error="provide left_speed+right_speed or direction")


@router.post("/stop", response_model=ControlActionResponse)
def control_stop(body: StopRequest = None):
    result = get_motion_controller().stop("rest_stop")
    return ControlActionResponse(**result)


@router.post("/brake", response_model=ControlActionResponse)
def control_brake(body: BrakeRequest = None):
    result = get_motion_controller().brake("rest_brake")
    return ControlActionResponse(**result)


@router.post("/estop", response_model=dict)
def control_estop():
    """Emergency stop — identical latch to POST /api/v1/robot/estop."""
    logger.critical("E-STOP triggered via /control/estop")
    result = motor_service.trigger_estop()
    get_motion_controller().stop("estop")
    return result


@router.post("/estop/reset", response_model=dict)
def control_estop_reset():
    return motor_service.reset_estop()


@router.get("/state", response_model=ControlStateResponse)
def control_state():
    return ControlStateResponse(**get_motion_controller().get_state())
