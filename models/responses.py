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


class CpuStatus(BaseModel):
    percent: Optional[float] = None
    count: Optional[int] = None
    freq_mhz: Optional[float] = None
    load_avg: Optional[List[float]] = None


class MemStatus(BaseModel):
    total_mb: Optional[float] = None
    used_mb: Optional[float] = None
    percent: Optional[float] = None


class DiskStatus(BaseModel):
    total_gb: Optional[float] = None
    used_gb: Optional[float] = None
    percent: Optional[float] = None
    free_gb: Optional[float] = None


class ProcessStatus(BaseModel):
    rss_mb: Optional[float] = None
    threads: Optional[int] = None
    connections: Optional[int] = None
    cpu_percent: Optional[float] = None
    uptime_s: Optional[int] = None


class SimulationStatus(BaseModel):
    """Phase 18: explicit simulation mode state."""
    simulation: bool
    env_var: str
    latched: bool
    hardware_disabled: bool
    preflight_stop: Optional[dict] = None


class SecurityFinding(BaseModel):
    id: str
    severity: str
    detail: str


class SecurityStatus(BaseModel):
    """Phase 20: startup security-check summary."""
    checked_at_startup: bool
    findings: List[SecurityFinding]
    count: int
    clean: bool


class SystemStatusResponse(BaseModel):
    """Phase 17: real host metrics. `null` = not measurable on this platform."""
    timestamp: int
    uptime_s: int
    cpu: CpuStatus
    memory: MemStatus
    swap: MemStatus
    disk: DiskStatus
    temperature_c: Optional[float] = None
    process: ProcessStatus
    simulation: SimulationStatus
    security: SecurityStatus
    source: str


# ── Auth ─────────────────────────────────────────────────────────────────────

class AuthSessionResponse(BaseModel):
    token: str
    role: str
    permissions: List[str]
    expires_in: int
    refresh_token: str = ""


class AuthRefreshResponse(BaseModel):
    token: str
    role: str
    permissions: List[str]
    expires_in: int
    refresh_token: str = ""


class AuthLogoutResponse(BaseModel):
    ok: bool
    revoked: int = 0


class DevicesManageResponse(BaseModel):
    ok: bool
    stats: dict


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
