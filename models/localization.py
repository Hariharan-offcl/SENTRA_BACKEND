"""
SENTRA — Localization models (Phase 5).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class TagEntry(BaseModel):
    tag_id: int = Field(..., ge=0, le=586)
    name: str
    type: str
    notes: str = ""
    created_at: float
    updated_at: float


class TagMapResponse(BaseModel):
    tags: List[TagEntry]
    count: int


class TagUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    type: str = Field(default="LOCATION", pattern="^(LOCATION|DOCK)$")
    notes: str = Field(default="", max_length=256)


class TagUpsertResponse(BaseModel):
    tag: TagEntry
    created: bool


class TagDeleteResponse(BaseModel):
    deleted: bool


class VisibleTag(BaseModel):
    tag_id: int
    name: Optional[str] = None
    type: Optional[str] = None
    registered: bool
    seen_at: float
    age_s: float
    distance_m: Optional[float] = None
    bearing_deg: Optional[float] = None
    confidence: float
    source: str


class LocalizationStatusResponse(BaseModel):
    located: bool
    last_known: Optional[VisibleTag] = None
    visible_tags: List[VisibleTag]
    localized_recently: bool
    detector: Dict


class DetectDebugResponse(BaseModel):
    detections: List[Dict]
    count: int
    detector: Dict
