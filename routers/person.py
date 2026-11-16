"""
Router: Person detection endpoints (Phase 10)
  GET  /api/v1/person/detections  — recent detection events (newest first)
  GET  /api/v1/person/tracked     — currently-tracked persons
  GET  /api/v1/person/status      — backend/pipeline health
  POST /api/v1/person/simulate    — inject synthetic persons (dev/emulator)

Person detection is modular: backends are swappable (simulation today, HOG
via env, ML models later) without API changes. Person recognition (names,
registration) arrives in Phase 11.
"""

import logging

from fastapi import APIRouter

from models.person import (
    PersonDetectionsResponse,
    PersonDetection,
    TrackedResponse,
    TrackedPerson,
    PersonStatsResponse,
    PersonSimulateRequest,
    PersonSimulateResponse,
)
from services import person_detection

router = APIRouter(prefix="/api/v1/person", tags=["Person Detection"])
logger = logging.getLogger(__name__)


@router.get("/detections", response_model=PersonDetectionsResponse)
def person_detections(limit: int = 20):
    items = person_detection.get_detections(limit)
    return PersonDetectionsResponse(detections=[PersonDetection(**d) for d in items],
                                    count=len(items))


@router.get("/tracked", response_model=TrackedResponse)
def person_tracked():
    tracked = person_detection.get_tracked()
    return TrackedResponse(tracked=[TrackedPerson(**t) for t in tracked],
                           count=len(tracked))


@router.get("/status", response_model=PersonStatsResponse)
def person_status():
    return PersonStatsResponse(**person_detection.stats())


@router.post("/simulate", response_model=PersonSimulateResponse)
def person_simulate(body: PersonSimulateRequest = None):
    """Dev/emulator hook: inject synthetic tracked persons."""
    body = body or PersonSimulateRequest()
    result = person_detection.inject(body.persons, body.confidence)
    return PersonSimulateResponse(**result)
