"""
SENTRA — Person detection models (Phase 10).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class PersonDetection(BaseModel):
    person_id: int
    bbox: List[int]
    confidence: float
    timestamp: float
    frame_id: str
    source: str


class PersonDetectionsResponse(BaseModel):
    detections: List[PersonDetection]
    count: int


class TrackedPerson(BaseModel):
    person_id: int
    bbox: List[int]
    confidence: float
    last_seen: float
    frames: int
    frame_id: str
    age_s: float


class TrackedResponse(BaseModel):
    tracked: List[TrackedPerson]
    count: int


class PersonStatsResponse(BaseModel):
    enabled: bool
    backend: str
    requested_backend: str
    resolved_backend: str = "unavailable"
    opencv_available: bool
    frames_seen: int
    tracked_count: int
    history_size: int
    min_confidence: float
    avg_detect_ms: Optional[float] = None
    last_detect_ms: Optional[float] = None
    hog_downscale_width: Optional[int] = None
    hog_winstride: Optional[int] = None


class PersonSimulateRequest(BaseModel):
    persons: int = Field(default=1, ge=1, le=5)
    confidence: float = Field(default=0.92, ge=0.1, le=1.0)


class PersonSimulateResponse(BaseModel):
    injected: int
    frame_id: str
