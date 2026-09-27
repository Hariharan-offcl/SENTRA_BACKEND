"""
Router: Emergency call endpoints (Phase 13)
  GET  /api/v1/emergency/status  — current/last emergency session + tuning
  POST /api/v1/emergency/ack     — caregiver acknowledges (ends ringing session)
  GET  /api/v1/emergency/history — past emergency sessions (most recent last)

The call itself reuses the Phase 1 call pipeline unchanged:
  invite over /ws/alerts → app connects /ws/webrtc/user + /ws/call/user.
"""

import logging

from fastapi import APIRouter

from models.emergency import (
    EmergencySession,
    EmergencyStatusResponse,
    EmergencyAckRequest,
    EmergencyAckResponse,
    EmergencyHistoryResponse,
)
from services import emergency_call

router = APIRouter(prefix="/api/v1/emergency", tags=["Emergency Call"])
logger = logging.getLogger(__name__)


@router.get("/status", response_model=EmergencyStatusResponse)
def emergency_status():
    return EmergencyStatusResponse(**emergency_call.get_status())


@router.post("/ack", response_model=EmergencyAckResponse)
def emergency_ack(body: EmergencyAckRequest = None):
    note = body.note if body else ""
    session = emergency_call.ack(note)
    logger.info("Emergency ack via REST (session=%s)",
                session.get("session_id") if session else None)
    return EmergencyAckResponse(ok=session is not None, session=session)


@router.get("/history", response_model=EmergencyHistoryResponse)
def emergency_history():
    return EmergencyHistoryResponse(
        sessions=[EmergencySession(**s) for s in emergency_call.get_history()])
