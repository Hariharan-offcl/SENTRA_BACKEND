"""
Router: Safety endpoints (Phase 3)
  GET  /api/v1/safety/status      — live gate status, sensors, thresholds
  GET  /api/v1/safety/events      — recent safety events + counters
  GET  /api/v1/safety/thresholds  — current runtime thresholds + bounds
  PUT  /api/v1/safety/thresholds  — update thresholds (validated, batch-atomic)
  POST /api/v1/safety/thresholds/reset — restore env-var defaults

Note: thresholds are in-memory (env defaults on restart) until persistence lands.
"""

import logging

from fastapi import APIRouter, Query

from core.safety import get_safety_layer
from core.safety_config import get_safety_config
from models.safety import (
    ThresholdsResponse,
    ThresholdLimitsResponse,
    ThresholdUpdateRequest,
    ThresholdUpdateResponse,
    SafetyStatusResponse,
    SafetyEventsResponse,
    SafetyEventItem,
)
from services import safety_events

router = APIRouter(prefix="/api/v1/safety", tags=["Safety"])
logger = logging.getLogger(__name__)


def _thresholds_response() -> ThresholdsResponse:
    snap = get_safety_config().snapshot()
    return ThresholdsResponse(
        front_obstacle_stop_m=snap["front_obstacle_stop_m"],
        rear_obstacle_stop_m=snap["rear_obstacle_stop_m"],
        cliff_stop=snap["cliff_stop"] > 0.5,
        patrol_obstacle_m=snap["patrol_obstacle_m"],
        manual_cmd_timeout_s=snap["manual_cmd_timeout_s"],
        autonomous_cmd_timeout_s=snap["autonomous_cmd_timeout_s"],
        accel_pct_per_s=snap["accel_pct_per_s"],
        decel_pct_per_s=snap["decel_pct_per_s"],
        max_speed_multiplier=snap["max_speed_multiplier"],
    )


@router.get("/status", response_model=SafetyStatusResponse)
def safety_status():
    """Live safety-layer status: gate decision, sensors, thresholds, watchdog."""
    return SafetyStatusResponse(**get_safety_layer().status())


@router.get("/events", response_model=SafetyEventsResponse)
def safety_events_list(limit: int = Query(default=50, ge=1, le=200)):
    history = safety_events.get_history(limit)
    counters = safety_events.get_counters()
    return SafetyEventsResponse(
        events=[SafetyEventItem(**e) for e in history],
        counters=counters,
    )


@router.get("/thresholds", response_model=ThresholdsResponse)
def get_thresholds():
    return _thresholds_response()


@router.get("/thresholds/limits", response_model=ThresholdLimitsResponse)
def get_threshold_limits():
    return ThresholdLimitsResponse(limits=get_safety_config().limits())


@router.put("/thresholds", response_model=ThresholdUpdateResponse)
def update_thresholds(body: ThresholdUpdateRequest):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    result = get_safety_config().update(updates)
    return ThresholdUpdateResponse(**result, thresholds=_thresholds_response())


@router.post("/thresholds/reset", response_model=ThresholdsResponse)
def reset_thresholds():
    get_safety_config().reset()
    return _thresholds_response()
