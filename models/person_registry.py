"""
SENTRA — Person registry/recognition models (Phase 11).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class PersonRecord(BaseModel):
    person_key: str
    name: str
    snapshot_path: Optional[str] = None
    embedding_count: int
    created_at: float
    updated_at: float


class PersonListResponse(BaseModel):
    persons: List[PersonRecord]
    count: int


class PersonRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class PersonRegisterResponse(BaseModel):
    ok: bool
    person: Optional[PersonRecord] = None
    faces_found: int = 0
    error: Optional[str] = None


class PersonDeleteResponse(BaseModel):
    deleted: bool


class RecognizeFace(BaseModel):
    bbox: List[int]
    known: bool
    match: Optional[Dict] = None


class RecognizeResponse(BaseModel):
    ok: bool
    faces: List[RecognizeFace]
    unknown_count: int
    error: Optional[str] = None


class MatchRequest(BaseModel):
    similarity: Optional[float] = None  # reserved for future thresholds


class RecognizeStatusResponse(BaseModel):
    enabled: bool
    backend: str
    opencv_available: bool
    haar_available: bool
    match_threshold: float
    registered_persons: int
    frames_seen: int
    faces_seen: int
    unknown_alerts: int
    unknown_cooldown_s: float
