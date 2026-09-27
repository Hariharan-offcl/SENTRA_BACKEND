"""
Router: Device management endpoints (Phase 15) — the scaling surface.
  GET    /api/v1/devices          — list every known device
  GET    /api/v1/devices/stats    — registry + token stats
  POST   /api/v1/devices/{id}/revoke   — kill all of a device's tokens
  POST   /api/v1/devices/{id}/unrevoke — allow re-login
  DELETE /api/v1/devices/{id}           — forget device (tokens revoked)
  GET    /api/v1/devices/{id}/tokens    — token audit rows for one device
  POST   /api/v1/devices/cleanup        — drop long-dead token rows

Designed for N caregiver phones / N rover nodes: registry-backed login
already creates device rows; these endpoints manage them.
"""

import logging

from fastapi import APIRouter, HTTPException

from models.devices import (
    DeviceItem,
    DeviceListResponse,
    DeviceStatsResponse,
    DeviceActionResponse,
)
from models.responses import DevicesManageResponse
from services import device_registry

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])
logger = logging.getLogger(__name__)


@router.get("", response_model=DeviceListResponse)
def list_devices():
    return DeviceListResponse(devices=[
        DeviceItem(**d) for d in device_registry.list_devices()])


@router.get("/stats", response_model=DeviceStatsResponse)
def device_stats():
    return DeviceStatsResponse(**device_registry.stats())


@router.post("/{device_id}/revoke", response_model=DeviceActionResponse)
def revoke_device(device_id: str):
    if device_registry.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    # Count live tokens first (set_device_revoked sweeps them internally).
    n = device_registry.revoke_device_tokens(device_id)
    rec = device_registry.set_device_revoked(device_id, True)
    logger.info("Device %s revoked (%d token(s) killed)", device_id, n)
    return DeviceActionResponse(ok=True, device=DeviceItem(**rec),
                                tokens_revoked=n)


@router.post("/{device_id}/unrevoke", response_model=DeviceActionResponse)
def unrevoke_device(device_id: str):
    rec = device_registry.set_device_revoked(device_id, False)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return DeviceActionResponse(ok=True, device=DeviceItem(**rec))


@router.delete("/{device_id}", response_model=DeviceActionResponse)
def delete_device(device_id: str):
    tokens = device_registry.list_tokens(device_id)
    if not device_registry.delete_device(device_id):
        raise HTTPException(status_code=404, detail="Unknown device")
    logger.info("Device %s deleted (%d token(s) revoked)", device_id, len(tokens))
    return DeviceActionResponse(ok=True, tokens_revoked=len(tokens))


@router.get("/{device_id}/tokens", response_model=list[dict])
def device_tokens(device_id: str):
    if device_registry.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return device_registry.list_tokens(device_id)


@router.post("/cleanup", response_model=DevicesManageResponse)
def cleanup_registry():
    removed = device_registry.cleanup()
    return DevicesManageResponse(ok=True, stats={
        "removed_token_rows": removed, **device_registry.stats()})
