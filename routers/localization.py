"""
Router: Localization endpoints (Phase 5)
  GET    /api/v1/localization/status            — where am I / visible tags
  GET    /api/v1/localization/tags              — the tag map
  PUT    /api/v1/localization/tags/{tag_id}     — create/update a tag binding
  DELETE /api/v1/localization/tags/{tag_id}     — remove a binding
  POST   /api/v1/localization/detect            — debug: detect tags in an uploaded JPEG
  GET    /api/v1/localization/vision-stats      — vision ingress health
"""

import logging

from fastapi import APIRouter, Request

from models.localization import (
    TagMapResponse,
    TagEntry,
    TagUpsertRequest,
    TagUpsertResponse,
    TagDeleteResponse,
    LocalizationStatusResponse,
    VisibleTag,
    DetectDebugResponse,
)
from services import tag_map, apriltag_service, localization_service, vision_service

router = APIRouter(prefix="/api/v1/localization", tags=["Localization"])
logger = logging.getLogger(__name__)


def _visible_tags() -> list[VisibleTag]:
    described = localization_service._visible_described()
    described_ids = {d["tag_id"] for d in described}
    # Include unregistered visible tags too (marked registered=False)
    extra = []
    for det in apriltag_service.get_visible():
        if det["tag_id"] not in described_ids:
            extra.append(VisibleTag(
                tag_id=det["tag_id"], name=None, type=None, registered=False,
                seen_at=det["timestamp"], age_s=round(
                    __import__("time").time() - det["timestamp"], 2),
                distance_m=det.get("distance_m"),
                bearing_deg=det.get("bearing_deg"),
                confidence=round(det.get("confidence") or 0.0, 3),
                source=det.get("source", "phone"),
            ))
    return [VisibleTag(**d) for d in described] + extra


@router.get("/status", response_model=LocalizationStatusResponse)
def localization_status():
    loc = localization_service.get_localization()
    last = loc["last_known"]
    return LocalizationStatusResponse(
        located=loc["located"],
        last_known=VisibleTag(**last) if last else None,
        visible_tags=_visible_tags(),
        localized_recently=loc["localized_recently"],
        detector=apriltag_service.stats(),
    )


@router.get("/tags", response_model=TagMapResponse)
def get_tag_map():
    tags = tag_map.list_tags()
    return TagMapResponse(tags=[TagEntry(**t) for t in tags], count=len(tags))


@router.put("/tags/{tag_id}", response_model=TagUpsertResponse)
def upsert_tag(tag_id: int, body: TagUpsertRequest):
    existed = tag_map.get_tag(tag_id) is not None
    entry = tag_map.upsert_tag(tag_id, body.name, body.type, body.notes)
    return TagUpsertResponse(tag=TagEntry(**entry), created=not existed)


@router.delete("/tags/{tag_id}", response_model=TagDeleteResponse)
def delete_tag(tag_id: int):
    return TagDeleteResponse(deleted=tag_map.delete_tag(tag_id))


@router.post("/detect", response_model=DetectDebugResponse)
async def detect_debug(request: Request):
    """
    Debug/testing: POST a JPEG body → AprilTag detections.
    Uses the same detector as the live vision pipeline.
    """
    import cv2
    import numpy as np

    raw = await request.body()
    detections: list[dict] = []
    if raw:
        arr = np.frombuffer(raw, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            detections = apriltag_service.process_frame(frame, frame_id="debug")
    return DetectDebugResponse(
        detections=detections, count=len(detections),
        detector=apriltag_service.stats(),
    )


@router.get("/vision-stats")
def vision_stats():
    return vision_service.stats()
