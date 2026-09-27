"""
SENTRA — Device management models (Phase 15).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class DeviceItem(BaseModel):
    device_id: str
    name: str
    kind: str = Field(..., description="node | user")
    platform: str
    first_seen: float
    last_seen: float
    revoked: bool


class DeviceListResponse(BaseModel):
    devices: List[DeviceItem]


class DeviceStatsResponse(BaseModel):
    devices_total: int
    devices_by_kind: Dict[str, int]
    tokens_tracked: int
    tokens_active: int
    path: str


class DeviceActionResponse(BaseModel):
    ok: bool
    device: Optional[DeviceItem] = None
    tokens_revoked: int = 0
