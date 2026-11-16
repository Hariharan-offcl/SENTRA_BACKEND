"""
SENTRA — Mapping models (Phase 6).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class CaptureContext(BaseModel):
    imu: Dict = {}
    wheel_encoders: Dict = {}
    front_distance_m: Optional[float] = None
    rear_distance_m: Optional[float] = None


class CaptureDetection(BaseModel):
    distance_m: Optional[float] = None
    bearing_deg: Optional[float] = None
    confidence: Optional[float] = None
    source: str = "phone"


class MapCapture(BaseModel):
    capture_id: str
    tag_id: int
    named: bool
    name: Optional[str] = None
    seen_count: int
    first_seen_at: float
    last_seen_at: float
    detection: CaptureDetection
    context: CaptureContext
    frame_path: Optional[str] = None


class MapSession(BaseModel):
    id: str
    active: bool
    started_by: str
    started_at: float
    captures: List[MapCapture]
    stopped_at: Optional[float] = None


class SessionResponse(BaseModel):
    ok: bool
    session: Optional[MapSession] = None
    already_active: bool = False
    already_stopped: bool = False
    error: Optional[str] = None


class MapOverviewResponse(BaseModel):
    active: bool
    session: Optional[MapSession] = None
    captures: List[MapCapture]
    unnamed_count: int
    named_count: int
    registered_tags: int
    ttl_s: float
    enabled: bool


class MapTagRequest(BaseModel):
    tag_id: int = Field(..., ge=0, le=586, description="AprilTag id to name")
    name: str = Field(..., min_length=1, max_length=64)
    type: str = Field(default="LOCATION", pattern="^(LOCATION|DOCK)$")
    notes: str = Field(default="", max_length=256)


class MapTagResponse(BaseModel):
    ok: bool
    tag: Dict
    named_from_capture: bool = False
    capture_id: Optional[str] = None
    error: Optional[str] = None


class CaptureDeleteResponse(BaseModel):
    deleted: bool


class MappingStatusResponse(BaseModel):
    enabled: bool
    active: bool
    session: Optional[MapSession] = None
    ttl_s: float
    snapshot_dir: str
