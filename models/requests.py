"""
Pydantic request models — every inbound JSON body is validated here.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional


# ── Auth ─────────────────────────────────────────────────────────────────────

class AuthSessionRequest(BaseModel):
    role: Literal["OWNER", "GUARD", "GUEST"]
    device_id: str


# ── Pairing ──────────────────────────────────────────────────────────────────

class PairRequest(BaseModel):
    ip_address: str
    app_instance_id: str


# ── Camera ───────────────────────────────────────────────────────────────────

class IRFilterRequest(BaseModel):
    enabled: bool


# ── Robot Control ─────────────────────────────────────────────────────────────

class RobotModeRequest(BaseModel):
    mode: Literal["PATROL", "MANUAL", "STANDBY"]


class SpeedRequest(BaseModel):
    speed_multiplier: float = Field(..., ge=0.0, le=2.0)
    target_mps: float = Field(..., ge=0.0, le=1.2)


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsUpdateRequest(BaseModel):
    scheduled_autonomous_patrol: Optional[bool] = None
    patrol_interval_hours: Optional[int] = None
    auto_ir_night_vision: Optional[bool] = None
    ir_threshold_lux: Optional[int] = None
    high_precision_lidar: Optional[bool] = None
    acoustic_intruder_alarm: Optional[bool] = None
    alarm_volume_db: Optional[int] = None
