"""
SENTRA — Patrol models (Phase 7).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class PatrolSession(BaseModel):
    id: str
    route: str
    waypoints: List[str]
    index: int
    active: bool
    started_at: float
    waypoint_started_at: float
    blocked: bool
    blocked_since: Optional[float] = None
    confirmed_waypoints: List[str]
    skipped_waypoints: List[str]
    mode: str
    last_decision: Optional[Dict] = None
    progress: Optional[str] = None
    current_waypoint: Optional[str] = None
    ended_reason: Optional[str] = None
    ended_at: Optional[float] = None


class PatrolStatusResponse(BaseModel):
    active: bool
    mode: str
    estop_active: bool
    session: Optional[PatrolSession] = None
    legacy_wander: bool
    default_route: Optional[str] = None
    routes_count: int


class PatrolStartRequest(BaseModel):
    route: Optional[str] = Field(default=None, description="Route name; omit for default")


class PatrolStartResponse(BaseModel):
    ok: bool
    error: Optional[str] = None
    legacy_wander: bool = False
    session: Optional[PatrolSession] = None


class PatrolStopResponse(BaseModel):
    ok: bool
    was_active: bool


class RouteEntry(BaseModel):
    name: str
    waypoints: List[str]
    waypoint_count: int
    is_default: bool


class RoutesListResponse(BaseModel):
    routes: List[RouteEntry]
    default: Optional[str] = None
    path: str


class RouteSaveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    waypoints: List[str] = Field(..., min_length=2, description="Ordered location names from the tag map")
    set_default: bool = False


class RouteSaveResponse(BaseModel):
    ok: bool
    created: bool = False
    name: Optional[str] = None
    waypoints: Optional[List[str]] = None
    error: Optional[str] = None


class RouteDeleteResponse(BaseModel):
    ok: bool
    error: Optional[str] = None


class DefaultRouteRequest(BaseModel):
    name: str
