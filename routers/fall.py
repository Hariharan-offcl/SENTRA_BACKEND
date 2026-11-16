"""
Router: Fall detection endpoints (Phase 12)
  GET  /api/v1/fall/status   — overall + per-person states + tuning
  POST /api/v1/fall/reset    — acknowledge/clear confirmed fall (post-emergency)
  POST /api/v1/fall/simulate — drive a synthetic fall through the pipeline (dev)

Design guarantee: FALL_CONFIRMED is produced ONLY from sustained temporal
evidence (lying aspect + stationary, ≥ CONFIRM_TIME_S). A single frame can
never trigger it. Confirmation fires emergency hooks once (Phase 13 auto-call).
"""

import logging

from fastapi import APIRouter

from models.fall import (
    FallStatusResponse,
    FallResetRequest,
    FallResetResponse,
    FallSimulateRequest,
    FallSimulateResponse,
)
from services import fall_detection

router = APIRouter(prefix="/api/v1/fall", tags=["Fall Detection"])
logger = logging.getLogger(__name__)


@router.get("/status", response_model=FallStatusResponse)
def fall_status():
    return FallStatusResponse(**fall_detection.get_status())


@router.post("/reset", response_model=FallResetResponse)
def fall_reset(body: FallResetRequest = None):
    person_id = body.person_id if body else None
    logger.info("Fall reset via REST (person=%s)", person_id)
    return FallResetResponse(ok=True, status=fall_detection.reset(person_id))


@router.post("/simulate", response_model=FallSimulateResponse)
def fall_simulate(body: FallSimulateRequest = None):
    body = body or FallSimulateRequest()
    result = fall_detection.simulate_fall(body.person_id, body.duration_s)
    return FallSimulateResponse(ok=True, **result)
