"""
SENTRA — Fall detection models (Phase 12).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class FallPersonState(BaseModel):
    person_id: int
    state: str
    since: float
    aspect: float
    confidence: float


class FallTuning(BaseModel):
    aspect_lying: float
    movement_px: float
    confirm_time_s: float
    min_confidence: float
    poll_s: float


class FallEvidence(BaseModel):
    person_id: int
    state: str
    aspect: float
    confidence: float
    confirmed_at: float
    sustained_s: float


class FallStatusResponse(BaseModel):
    enabled: bool
    overall: str = Field(..., description="NORMAL | POSSIBLE_FALL | FALL_CONFIRMED")
    persons: List[FallPersonState]
    last_confirmed: Optional[FallEvidence] = None
    tuning: FallTuning


class FallResetRequest(BaseModel):
    person_id: Optional[int] = Field(default=None, description="omit to reset all")


class FallResetResponse(BaseModel):
    ok: bool
    status: FallStatusResponse


class FallSimulateRequest(BaseModel):
    person_id: int = Field(default=99, ge=1)
    duration_s: float = Field(default=5.0, gt=0, le=30)


class FallSimulateResponse(BaseModel):
    ok: bool
    person_id: int
    final_state: str
    evidence: Dict
