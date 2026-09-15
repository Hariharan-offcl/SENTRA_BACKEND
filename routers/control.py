"""
Router: Robot Control REST endpoints
  POST /api/v1/robot/mode
  POST /api/v1/robot/estop
  POST /api/v1/robot/estop/reset
  POST /api/v1/robot/speed
"""

import logging

from fastapi import APIRouter

from models.requests import RobotModeRequest, SpeedRequest
from models.responses import RobotModeResponse, EStopResponse, SpeedResponse
from services import motor_service

router = APIRouter(prefix="/api/v1/robot", tags=["Control"])
logger = logging.getLogger(__name__)


@router.post("/mode", response_model=RobotModeResponse)
def set_mode(body: RobotModeRequest):
    """Switch operational mode between PATROL, MANUAL, and STANDBY."""
    result = motor_service.set_mode(body.mode)
    return RobotModeResponse(**result)


@router.post("/estop", response_model=EStopResponse)
def emergency_stop():
    """
    EMERGENCY E-STOP — immediately kills all motor PWM signals.
    Target hardware latency: < 5 ms from HTTP receipt to GPIO state change.
    """
    result = motor_service.trigger_estop()
    logger.critical("E-STOP triggered via REST")
    return EStopResponse(**result)


@router.post("/estop/reset", response_model=EStopResponse)
def reset_estop():
    """Clear the E-STOP latch and re-enable motor commands."""
    result = motor_service.reset_estop()
    return EStopResponse(**result)


@router.post("/speed", response_model=SpeedResponse)
def set_speed(body: SpeedRequest):
    """Adjust the global speed throttle multiplier and target velocity."""
    result = motor_service.set_speed(body.speed_multiplier, body.target_mps)
    return SpeedResponse(**result)
