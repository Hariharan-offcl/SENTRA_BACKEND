"""
Router: Patrol endpoints (Phase 7)
  POST /api/v1/patrol/start          — start patrol (route or default; legacy wander if none)
  POST /api/v1/patrol/stop           — stop patrol
  GET  /api/v1/patrol/status         — session progress, blocked state, mode
  POST /api/v1/patrol/routes         — create/update a route (validated vs tag map)
  GET  /api/v1/patrol/routes         — list routes
  POST /api/v1/patrol/routes/default — set default route
  DELETE /api/v1/patrol/routes/{name} — delete a route

Safety: patrol drives through the centralized safety gate only; obstacles
STOP the rover (never steer around); manual takeover ends the patrol.
"""

import logging

from fastapi import APIRouter

from models.patrol import (
    PatrolStatusResponse,
    PatrolStartRequest,
    PatrolStartResponse,
    PatrolStopResponse,
    RoutesListResponse,
    RouteEntry,
    RouteSaveRequest,
    RouteSaveResponse,
    RouteDeleteResponse,
    DefaultRouteRequest,
)
from services import patrol_service

router = APIRouter(prefix="/api/v1/patrol", tags=["Patrol"])
logger = logging.getLogger(__name__)


@router.post("/start", response_model=PatrolStartResponse)
def patrol_start(body: PatrolStartRequest = None):
    route = body.route if (body and body.route) else None
    result = patrol_service.start_patrol(route)
    if result.get("ok") and result.get("session"):
        return PatrolStartResponse(ok=True, session=result["session"])
    return PatrolStartResponse(ok=result.get("ok", False),
                               error=result.get("error"),
                               legacy_wander=result.get("legacy_wander", False))


@router.post("/stop", response_model=PatrolStopResponse)
def patrol_stop():
    return PatrolStopResponse(**patrol_service.stop_patrol())


@router.get("/status", response_model=PatrolStatusResponse)
def patrol_status():
    return PatrolStatusResponse(**patrol_service.status())


@router.post("/routes", response_model=RouteSaveResponse)
def save_route(body: RouteSaveRequest):
    result = patrol_service.save_route(body.name, body.waypoints, body.set_default)
    return RouteSaveResponse(**result)


@router.get("/routes", response_model=RoutesListResponse)
def list_routes():
    routes = patrol_service.list_routes()
    default = patrol_service.get_default_route()
    entries = [RouteEntry(name=name, waypoints=wps, waypoint_count=len(wps),
                          is_default=(name == default))
               for name, wps in sorted(routes.items())]
    return RoutesListResponse(routes=entries, default=default,
                              path=patrol_service.routes_path())


@router.post("/routes/default", response_model=RouteSaveResponse)
def set_default(body: DefaultRouteRequest):
    result = patrol_service.set_default_route(body.name)
    return RouteSaveResponse(**result)


@router.delete("/routes/{name}", response_model=RouteDeleteResponse)
def delete_route(name: str):
    return RouteDeleteResponse(**patrol_service.delete_route(name))
