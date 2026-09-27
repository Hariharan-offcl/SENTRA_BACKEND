"""
SENTRA — Emergency call models (Phase 13).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class EmergencyTuning(BaseModel):
    ring_timeout_s: float
    cooldown_s: float


class EmergencySession(BaseModel):
    session_id: str
    state: str = Field(..., description="RINGING | ACTIVE | MISSED | ENDED")
    reason: str = "FALL"
    acknowledged: bool
    note: str = ""
    started_at: float
    ended_at: Optional[float] = None
    node_connected: bool = False
    user_connected: bool = False
    evidence: Dict


class EmergencyStatusResponse(BaseModel):
    enabled: bool
    session: Optional[EmergencySession] = None
    last_session: Optional[EmergencySession] = None
    tuning: EmergencyTuning


class EmergencyAckRequest(BaseModel):
    note: str = ""


class EmergencyAckResponse(BaseModel):
    ok: bool
    session: Optional[EmergencySession] = None


class EmergencyHistoryResponse(BaseModel):
    sessions: List[EmergencySession]
