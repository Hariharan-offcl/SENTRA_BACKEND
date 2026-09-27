"""
SENTRA — Safety models (Phase 3).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class ThresholdsResponse(BaseModel):
    """Runtime safety thresholds (all env-tunable at startup, API-tunable live)."""
    front_obstacle_stop_m: float
    rear_obstacle_stop_m: float
    cliff_stop: bool
    patrol_obstacle_m: float
    manual_cmd_timeout_s: float
    autonomous_cmd_timeout_s: float
    accel_pct_per_s: float
    decel_pct_per_s: float
    max_speed_multiplier: float


class ThresholdLimitsResponse(BaseModel):
    limits: Dict[str, List[float]]


class ThresholdUpdateRequest(BaseModel):
    """Partial update — only provided fields are applied (all-or-nothing per batch)."""
    front_obstacle_stop_m: Optional[float] = Field(default=None, ge=0.05, le=2.5)
    rear_obstacle_stop_m: Optional[float] = Field(default=None, ge=0.05, le=2.5)
    cliff_stop: Optional[bool] = None
    patrol_obstacle_m: Optional[float] = Field(default=None, ge=0.15, le=2.5)
    manual_cmd_timeout_s: Optional[float] = Field(default=None, ge=0.1, le=6.0)
    autonomous_cmd_timeout_s: Optional[float] = Field(default=None, ge=0.4, le=12.0)
    accel_pct_per_s: Optional[float] = Field(default=None, ge=10, le=600)
    decel_pct_per_s: Optional[float] = Field(default=None, ge=40, le=1200)
    max_speed_multiplier: Optional[float] = Field(default=None, ge=0.05, le=1.0)


class ThresholdUpdateResponse(BaseModel):
    updated: List[str]
    rejected: Dict[str, str]
    thresholds: ThresholdsResponse


class SafetyStatusResponse(BaseModel):
    mode: str
    estop_active: bool
    motors_disabled: bool
    moving: bool
    watchdog_active: bool
    last_decision: Dict
    sensors: Dict
    thresholds: ThresholdsResponse


class SafetyEventItem(BaseModel):
    id: str
    type: str
    severity: str
    location: str
    detail: Dict
    timestamp: float


class SafetyEventsResponse(BaseModel):
    events: List[SafetyEventItem]
    counters: Dict[str, int]
