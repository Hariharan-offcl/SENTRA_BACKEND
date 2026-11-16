"""
Router: Settings REST endpoints
  GET /api/v1/settings
  PUT /api/v1/settings
"""

import logging

from fastapi import APIRouter

from models.requests import SettingsUpdateRequest
from models.responses import SettingsResponse, SettingsUpdateResponse

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])
logger = logging.getLogger(__name__)

# ── In-memory settings store ─────────────────────────────────────────────────
# On the Pi, persist to /var/sentra/settings.json or SQLite.

_settings: dict = {
    "scheduled_autonomous_patrol": True,
    "patrol_interval_hours": 2,
    "auto_ir_night_vision": True,
    "ir_threshold_lux": 10,
    "high_precision_lidar": True,
    "acoustic_intruder_alarm": True,
    "alarm_volume_db": 95,
}


@router.get("", response_model=SettingsResponse)
def get_settings():
    """Read the current hardware & behaviour configuration."""
    return SettingsResponse(**_settings)


@router.put("", response_model=SettingsUpdateResponse)
def update_settings(body: SettingsUpdateRequest):
    """Merge provided fields into the active settings store."""
    updates = body.model_dump(exclude_none=True)
    _settings.update(updates)
    logger.info("Settings updated: %s", updates)
    return SettingsUpdateResponse(updated=True, message="Settings applied successfully")
