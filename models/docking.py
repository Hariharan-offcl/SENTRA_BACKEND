"""
SENTRA — Docking models (Phase 8).
"""

from pydantic import BaseModel
from typing import Optional


class DockSession(BaseModel):
    id: str
    state: str
    started_at: float
    state_started_at: float
    tag_id: int
    blocked_since: Optional[float] = None
    search_direction: int
    docked_by: str
    age_s: Optional[float] = None
    ended_reason: Optional[str] = None


class DockReturnRequest(BaseModel):
    # No fields needed today; present so an empty JSON body is accepted.
    pass


class DockReturnResponse(BaseModel):
    ok: bool
    error: Optional[str] = None
    session: Optional[DockSession] = None


class DockCancelResponse(BaseModel):
    ok: bool
    was_active: bool


class DockStatusResponse(BaseModel):
    active: bool
    session: Optional[DockSession] = None
    dock_tag_id: Optional[int] = None
    mode: Optional[str] = None
