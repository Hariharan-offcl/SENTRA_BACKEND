"""
Router: Manual mapping endpoints (Phase 6)
  POST   /api/v1/map/start          — begin a mapping session (MANUAL driving)
  POST   /api/v1/map/stop           — end the session
  GET    /api/v1/map                — overview: session + captures + tag count
  POST   /api/v1/map/tag            — name a captured tag → persistent tag map
  DELETE /api/v1/map/tag/{id}       — (legacy id form) same as naming fallback:
                                     deletes the BINDING from the tag map
  DELETE /api/v1/map/captures/{capture_id} — discard an unnamed capture

Mapping flow (Flutter):
    1. POST /map/start            → session begins (rover must be in MANUAL;
                                     started automatically if in STANDBY)
    2. Drive with the joystick    → tags seen by the phone camera appear as
                                     unnamed captures in GET /map
    3. POST /map/tag {"tag_id":2, "name":"Kitchen"} → binding persists
    4. POST /map/stop             → session ends; bindings remain
"""

import logging

from fastapi import APIRouter

from models.mapping import (
    SessionResponse,
    MapOverviewResponse,
    MapTagRequest,
    MapTagResponse,
    CaptureDeleteResponse,
    MappingStatusResponse,
    MapSession,
    MapCapture,
)
from services import mapping_service, tag_map

router = APIRouter(prefix="/api/v1/map", tags=["Mapping"])
logger = logging.getLogger(__name__)


def _session_model(sess: dict | None) -> MapSession | None:
    if sess is None:
        return None
    return MapSession(
        id=sess["id"], active=sess["active"], started_by=sess["started_by"],
        started_at=sess["started_at"],
        captures=[MapCapture(**c) for c in sess["captures"]],
        stopped_at=sess.get("stopped_at"),
    )


@router.post("/start", response_model=SessionResponse)
def start_mapping():
    result = mapping_service.start_session(started_by="rest")
    return SessionResponse(
        ok=result["ok"],
        session=_session_model(result.get("session")),
        already_active=result.get("already_active", False),
        error=result.get("error"),
    )


@router.post("/stop", response_model=SessionResponse)
def stop_mapping():
    result = mapping_service.stop_session()
    return SessionResponse(
        ok=result["ok"],
        session=_session_model(result.get("session")),
        already_stopped=result.get("already_stopped", False),
    )


@router.get("", response_model=MapOverviewResponse)
def map_overview():
    sess = mapping_service.get_session()
    captures = mapping_service.list_captures()
    unnamed = sum(1 for c in captures if not c["named"])
    return MapOverviewResponse(
        active=sess is not None,
        session=_session_model(sess),
        captures=[MapCapture(**c) for c in captures],
        unnamed_count=unnamed,
        named_count=len(captures) - unnamed,
        registered_tags=tag_map.count(),
        ttl_s=mapping_service.CAPTURE_TTL_S,
        enabled=mapping_service.MAPPING_ENABLED,
    )


@router.post("/tag", response_model=MapTagResponse)
def name_map_tag(body: MapTagRequest):
    """
    Name a pending capture for tag_id — or create the binding directly if no
    capture exists. Persists to the tag map (survives restarts).
    """
    result = mapping_service.name_capture(body.tag_id, body.name, body.type, body.notes)
    return MapTagResponse(**result)


@router.delete("/tag/{tag_id}", response_model=CaptureDeleteResponse)
def delete_map_tag(tag_id: int):
    """Delete a tag binding from the persistent map (Phase 6 spec endpoint)."""
    return CaptureDeleteResponse(deleted=tag_map.delete_tag(tag_id))


@router.delete("/captures/{capture_id}", response_model=CaptureDeleteResponse)
def delete_capture(capture_id: str):
    """Discard an unnamed pending capture (and its frame file)."""
    return CaptureDeleteResponse(deleted=mapping_service.delete_capture(capture_id))


@router.get("/status", response_model=MappingStatusResponse)
def mapping_status():
    st = mapping_service.stats()
    return MappingStatusResponse(
        enabled=st["enabled"], active=st["active"],
        session=_session_model(st["session"]),
        ttl_s=st["ttl_s"], snapshot_dir=st["snapshot_dir"],
    )
