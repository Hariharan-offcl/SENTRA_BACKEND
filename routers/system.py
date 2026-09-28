"""
Router: System endpoints
  GET  /api/v1/ping
  GET  /api/v1/system/info
  GET  /api/v1/system/capabilities
  GET  /api/v1/system/status      (Phase 17: real CPU/RAM/temp/disk metrics)
  POST /api/v1/system/reboot
"""

import asyncio
import logging
import time

from fastapi import APIRouter

from config import settings
from models.responses import (
    PingResponse,
    SystemInfoResponse,
    CapabilitiesResponse,
    RebootResponse,
    SystemStatusResponse,
)

router = APIRouter(prefix="/api/v1", tags=["System"])
logger = logging.getLogger(__name__)


@router.get("/ping", response_model=PingResponse)
def ping():
    """Health check — used by the Splash screen on app startup."""
    return PingResponse(
        status="online",
        unit_id=settings.unit_id,
        timestamp=int(time.time()),
    )


@router.get("/system/info", response_model=SystemInfoResponse)
def system_info():
    """Hardware & firmware info — used by the Welcome screen."""
    return SystemInfoResponse(
        unit_name=settings.unit_name,
        hardware=settings.hardware,
        firmware_version=settings.firmware_version,
        serial_number=settings.serial_number,
        api_version=settings.api_version,
    )


@router.get("/system/capabilities", response_model=CapabilitiesResponse)
def system_capabilities():
    """Sensor & hardware capability discovery — used by the Permissions screen."""
    return CapabilitiesResponse(
        local_network_mdns=True,
        camera_installed=True,
        camera_resolution="1080p",
        lidar_installed=True,
        night_vision_ir=True,
        acoustic_alarm_speaker=True,
    )


@router.get("/system/status", response_model=SystemStatusResponse)
def system_status():
    """
    Phase 17: real host health snapshot — CPU, RAM, swap, disk, SoC
    temperature, load and backend-process stats. psutil-backed; fields the
    platform cannot measure come back null (e.g. temperature on Windows dev).
    Used by the app's Diagnostics screen and by relay remote monitoring.
    """
    from services import system_metrics  # lazy: keeps router import cheap
    return SystemStatusResponse(**system_metrics.get_system_status())


@router.post("/system/reboot", response_model=RebootResponse)
async def system_reboot():
    """
    Trigger a Raspberry Pi OS reboot after a 3-second delay.
    On the Pi this executes `sudo reboot`; the Pi OS handles the rest.
    """
    async def _reboot_after_delay():
        await asyncio.sleep(3)
        import subprocess  # noqa: PLC0415
        try:
            subprocess.run(["sudo", "reboot"], check=True)
        except Exception as exc:
            logger.error("Reboot failed: %s", exc)

    asyncio.create_task(_reboot_after_delay())
    logger.warning("Reboot scheduled in 3 seconds")
    return RebootResponse(
        reboot_initiated=True,
        message="Raspberry Pi 5 rebooting in 3 seconds...",
    )
