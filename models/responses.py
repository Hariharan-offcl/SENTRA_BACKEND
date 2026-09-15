"""
Pydantic response models — every outbound JSON body is typed here.
"""

from pydantic import BaseModel
from typing import List, Optional


# ── System ───────────────────────────────────────────────────────────────────

class PingResponse(BaseModel):
    status: str
    unit_id: str
    timestamp: int


class SystemInfoResponse(BaseModel):
    unit_name: str
    hardware: str
    firmware_version: str
    serial_number: str
    api_version: str


class CapabilitiesResponse(BaseModel):
    local_network_mdns: bool
    camera_installed: bool
    camera_resolution: str
    lidar_installed: bool
    night_vision_ir: bool
    acoustic_alarm_speaker: bool


class RebootResponse(BaseModel):
    reboot_initiated: bool
    message: str


# ── Auth ─────────────────────────────────────────────────────────────────────

class AuthSessionResponse(BaseModel):
    token: str
    role: str
    permissions: List[str]
    expires_in: int


# ── Pairing ──────────────────────────────────────────────────────────────────

class PairResponse(BaseModel):
    paired: bool
    unit_id: str
    unit_name: str
    websocket_telemetry_url: str
    websocket_control_url: str
    stream_url: str


# ── Telemetry ─────────────────────────────────────────────────────────────────

class InitialSyncResponse(BaseModel):
    hardware_model: str
    ip_address: str
    ping_latency_ms: int
    battery_level: int
    battery_charging: bool
    status_badge: str


class BatteryInfo(BaseModel):
    level: int
    charging: bool
    delta: str


class LiveTelemetryResponse(BaseModel):
    operational_state: str
    zone: str
    status: str
    battery: BatteryInfo
    latency_ms: int
    uptime_hours: float
    patrol_speed_mps: float


# ── Camera ───────────────────────────────────────────────────────────────────

class IRFilterResponse(BaseModel):
    ir_filter_active: bool
    mode: str


class SnapshotResponse(BaseModel):
    file_path: str
    download_url: str


# ── Robot Control ─────────────────────────────────────────────────────────────

class RobotModeResponse(BaseModel):
    active_mode: str
    status: str


class EStopResponse(BaseModel):
    estop_active: bool
    motors_disabled: bool
    status: str


class SpeedResponse(BaseModel):
    speed_multiplier: float
    max_speed_mps: float


# ── Alerts ───────────────────────────────────────────────────────────────────

class AlertItem(BaseModel):
    id: str
    title: str
    timestamp: str
    description: str
    severity: str
    acknowledged: bool


class AlertsListResponse(BaseModel):
    alerts: List[AlertItem]


class AckAlertResponse(BaseModel):
    alert_id: str
    acknowledged: bool


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsResponse(BaseModel):
    scheduled_autonomous_patrol: bool
    patrol_interval_hours: int
    auto_ir_night_vision: bool
    ir_threshold_lux: int
    high_precision_lidar: bool
    acoustic_intruder_alarm: bool
    alarm_volume_db: int


class SettingsUpdateResponse(BaseModel):
    updated: bool
    message: str
