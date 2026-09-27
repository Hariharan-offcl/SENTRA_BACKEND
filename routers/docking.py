"""
Router: Return-to-dock endpoints (Phase 8)
  POST /api/v1/dock/return  — start tag-guided return to dock
  POST /api/v1/dock/cancel  — cancel an active docking run
  GET  /api/v1/dock/status  — session state (SEEK/APPROACH/ALIGN/DOCKED), dock tag

Flow: rotate-search for the DOCK tag → approach steered by tag bearing
(slow near) → align within tolerance → stop in STANDBY. Manual takeover or
e-stop cancels at any point. All motion passes the centralized safety gate.
"""

import logging

from fastapi import APIRouter

from core.state import get_robot_state
from models.docking import (
    DockReturnRequest,
    DockReturnResponse,
    DockCancelResponse,
    DockStatusResponse,
    DockSession,
)
from services import docking_service

router = APIRouter(prefix="/api/v1/dock", tags=["Docking"])
logger = logging.getLogger(__name__)


def _session_model(sess: dict | None) -> DockSession | None:
    return DockSession(**sess) if sess else None


@router.post("/return", response_model=DockReturnResponse)
def dock_return(body: DockReturnRequest = None):
    result = docking_service.start_return(docked_by="rest")
    return DockReturnResponse(
        ok=result.get("ok", False),
        error=result.get("error"),
        session=_session_model(result.get("session")),
    )


@router.post("/cancel", response_model=DockCancelResponse)
def dock_cancel():
    return DockCancelResponse(**docking_service.cancel_return("rest_cancel"))


@router.get("/status", response_model=DockStatusResponse)
def dock_status():
    st = get_robot_state()
    sess = docking_service.status()
    return DockStatusResponse(
        active=sess is not None,
        session=_session_model(sess),
        dock_tag_id=docking_service.dock_tag_id(),
        mode=st.get_mode(),
    )
