"""
SENTRA — Manual control request/response models (Phase 2).

One endpoint (/control/move) accepts several forms:
    {"left_speed": 50, "right_speed": 50, "duration": 2.0}
    {"left_speed": -50, "right_speed": 50}               # rotate left (brief)
    {"direction": "FORWARD", "scale": 0.5, "duration": 1.5}
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal


class MoveRequest(BaseModel):
    left_speed: Optional[float] = Field(default=None, ge=-100, le=100,
                                        description="Left wheel duty % (-100..100)")
    right_speed: Optional[float] = Field(default=None, ge=-100, le=100,
                                         description="Right wheel duty % (-100..100)")
    direction: Optional[Literal["FORWARD", "BACKWARD", "LEFT", "RIGHT"]] = None
    scale: float = Field(default=0.5, ge=0.0, le=1.0,
                         description="Magnitude for directional moves")
    duration: Optional[float] = Field(default=None, gt=0, le=30,
                                      description="Seconds to move (required for directional one-shots)")


class StopRequest(BaseModel):
    # No fields needed; present so an empty JSON body is accepted cleanly.
    pass


class BrakeRequest(BaseModel):
    pass


class ControlActionResponse(BaseModel):
    applied: bool
    action: str
    left: Optional[float] = None
    right: Optional[float] = None
    error: Optional[str] = None


class ControlStateResponse(BaseModel):
    current_left: float
    current_right: float
    target_left: float
    target_right: float
    braking: bool
    active: bool
    mode: str
    estop_active: bool
    moving: bool
    speed_multiplier: float
