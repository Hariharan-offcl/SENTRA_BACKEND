"""
Router: Manual mapping endpoints (Phase 6)
  POST   /api/v1/map/start          — begin a mapping session (MANUAL driving)
  POST   /api/v1/map/stop           — end the session
  GET    /api/v1/map/current        — get current relative coordinates
  GET    /api/v1/map                — overview: session + captures + tag count
  POST   /api/v1/map/tag            — name a captured tag → persistent tag map
  DELETE /api/v1/map/tag/{id}       — (legacy id form) same as naming fallback:
                                     deletes the BINDING from the tag map
  DELETE /api/v1/map/captures/{capture_id} — discard an unnamed capture
"""

import logging

from fastapi import APIRouter, HTTPException
from services.mapping_service import mapping_service

router = APIRouter(prefix="/api/v1/map", tags=["Mapping"])
logger = logging.getLogger(__name__)

@router.post("/start")
async def start_mapping():
    """Begin a manual mapping session."""
    result = mapping_service.start_session()
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.post("/stop")
async def stop_mapping():
    """End the current mapping session."""
    result = mapping_service.stop_session()
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/current")
async def get_mapping_pos():
    """Get current relative coordinates during a session."""
    return mapping_service.get_current_position()

# Keep other endpoints for backward compatibility
from models.mapping import (
    SessionResponse, MapOverviewResponse, MapTagRequest,
    MapTagResponse, CaptureDeleteResponse, MappingStatusResponse,
    MapSession, MapCapture,
)
from services import tag_map

def _session_model(sess: dict | None) -> MapSession | None:
    if sess is None: return None
    return MapSession(
        id=sess["id"], active=sess["active"], started_by=sess["started_by"],
        started_at=sess["started_at"],
        captures=[MapCapture(**c) for c in sess["captures"]],
        stopped_at=sess.get("stopped_at"),
    )

@router.get("", response_model=MapOverviewResponse)
def map_overview():
    sess = mapping_service.get_session() if hasattr(mapping_service, "get_session") else None
    captures = mapping_service.list_captures() if hasattr(mapping_service, "list_captures") else []
    unnamed = sum(1 for c in captures if not c.get("named"))
    return MapOverviewResponse(
        active=sess is not None, session=_session_model(sess),
        captures=[MapCapture(**c) for c in captures],
        unnamed_count=unnamed, named_count=len(captures) - unnamed,
        registered_tags=tag_map.count(), ttl_s=10.0, enabled=True,
    )

@router.post("/tag", response_model=MapTagResponse)
def name_map_tag(body: MapTagRequest):
    result = mapping_service.name_capture(body.tag_id, body.name, body.type, body.notes)
    return MapTagResponse(**result)

@router.delete("/tag/{tag_id}", response_model=CaptureDeleteResponse)
def delete_map_tag(tag_id: int):
    return CaptureDeleteResponse(deleted=tag_map.delete_tag(tag_id))

@router.delete("/captures/{capture_id}", response_model=CaptureDeleteResponse)
def delete_capture(capture_id: str):
    return CaptureDeleteResponse(deleted=mapping_service.delete_capture(capture_id))

@router.get("/status", response_model=MappingStatusResponse)
def mapping_status():
    st = mapping_service.stats()
    return MappingStatusResponse(
        enabled=True, active=mapping_service._active,
        session=_session_model(mapping_service.get_session() if hasattr(mapping_service, "get_session") else None),
        ttl_s=10.0, snapshot_dir=core_config.SENTRA_MAP_SNAPSHOT_DIR,
    )
