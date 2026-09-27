"""
Router: Telemetry REST endpoints
  GET /api/v1/telemetry/initial-sync
  GET /api/v1/telemetry/live
  GET /api/v1/telemetry/sensors     (Phase 4: unified sensor snapshot + health)
"""

import logging

from fastapi import APIRouter

from models.responses import InitialSyncResponse, LiveTelemetryResponse, BatteryInfo
from services import telemetry_service

router = APIRouter(prefix="/api/v1/telemetry", tags=["Telemetry"])
logger = logging.getLogger(__name__)


@router.get("/initial-sync", response_model=InitialSyncResponse)
def initial_sync():
    """
    Hardware verification & initial battery sync.
    Called by the Ready & Connected screen immediately after pairing.
    """
    data = telemetry_service.get_initial_sync()
    return InitialSyncResponse(**data)


@router.get("/live", response_model=LiveTelemetryResponse)
def live_telemetry():
    """
    Live snapshot of operational state, battery, latency and uptime.
    Used by the Dashboard as a REST fallback / first load.
    """
    data = telemetry_service.get_live_telemetry()
    return LiveTelemetryResponse(
        operational_state=data["operational_state"],
        zone=data["zone"],
        status=data["status"],
        battery=BatteryInfo(**data["battery"]),
        latency_ms=data["latency_ms"],
        uptime_hours=data["uptime_hours"],
        patrol_speed_mps=data["patrol_speed_mps"],
    )


@router.get("/sensors")
def unified_sensors():
    """
    Phase 4: unified sensor snapshot (ultrasonic, cliff, IMU, encoders)
    with per-sensor health (FRESH / STALE / SIMULATED / DISABLED / NO_DATA).
    Built by the background aggregator — never blocks on hardware.
    """
    from services import sensor_service
    return {
        "sensors": sensor_service.get_snapshot(),
        "health": sensor_service.get_health(),
    }
