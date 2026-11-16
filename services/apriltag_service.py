"""
SENTRA — AprilTag detection service (Phase 5).

Consumes frames subscribed from the vision service (phone-camera ingress) and
detects AprilTag 36h11 markers with OpenCV's Aruco detector. Results carry:
tag_id, timestamp, relative distance (if camera intrinsics are configured),
bearing, orientation, confidence, and frame metadata.

Distance/bearing model: with known camera FOV and tag size, tag apparent size
gives distance; tag center offset from image center gives bearing. Without
intrinsics config those fields are None — detection still works.

Simulation: `inject_detection()` lets tests/dev push synthetic tags through
the same pipeline (SENTRA_SIMULATION or explicit call).

Env vars:
    SENTRA_APRILTAG_ENABLED   (default true)
    SENTRA_APRILTAG_FAMILY    (default 36h11; accepted: 36h11, 36h10)
    SENTRA_TAG_SIZE_M         (default 0.10 — printed tag edge length)
    SENTRA_CAM_HFOV_DEG       (default 62.0 — typical phone main camera)
    SENTRA_CAM_VFOV_DEG       (default 48.0)
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    _CV_OK = True
except ImportError:
    _CV_OK = False

APRILTAG_ENABLED = os.getenv("SENTRA_APRILTAG_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
FAMILY_NAME = os.getenv("SENTRA_APRILTAG_FAMILY", "36h11").strip().lower()
TAG_SIZE_M = float(os.getenv("SENTRA_TAG_SIZE_M", "0.10"))
CAM_HFOV_DEG = float(os.getenv("SENTRA_CAM_HFOV_DEG", "62.0"))
CAM_VFOV_DEG = float(os.getenv("SENTRA_CAM_VFOV_DEG", "48.0"))
HISTORY_MAX = 100
DETACH_OLDER_THAN_S = 3.0   # a tag not seen for this long is 'lost'

_lock = threading.RLock()
_history: deque = deque(maxlen=HISTORY_MAX)
_last_by_tag: dict[int, dict] = {}
_frame_results: deque = deque(maxlen=10)   # [{frame_id, timestamp, results}] (Ph.6)
_last_frame_at: float = 0.0
_detections_enabled: bool = True
_frames_seen: int = 0
_simulated: bool = not _CV_OK


_cached_detector = None


def _detector():
    """Build (once) and return the Aruco/AprilTag detector (OpenCV 4.7+ API)."""
    global _cached_detector
    if _cached_detector is None:
        family_map = {
            "36h11": getattr(cv2.aruco, "DICT_APRILTAG_36h11", None),
            "36h10": getattr(cv2.aruco, "DICT_APRILTAG_36h10", None),
        }
        dict_id = family_map.get(FAMILY_NAME) or cv2.aruco.DICT_APRILTAG_36h11
        dictionary = cv2.aruco.getPredefinedDictionary(dict_id)
        params = cv2.aruco.DetectorParameters()
        _cached_detector = cv2.aruco.ArucoDetector(dictionary, params)
    return _cached_detector


def process_frame(frame, frame_id: str = "", source: str = "phone") -> list[dict]:
    """
    Detect AprilTags in one BGR frame. Returns a list of detection dicts.
    Thread-safe; called by the vision service on every subscribed frame.
    """
    global _last_frame_at, _frames_seen, _simulated
    if not APRILTAG_ENABLED or not _CV_OK or frame is None:
        return []

    now = time.time()
    results = []
    try:
        detector = _detector()
        corners, ids, rejected = detector.detectMarkers(frame)
        h, w = frame.shape[:2]
        cx = w / 2.0
        if ids is not None:
            for corner, tag_id in zip(corners, ids.flatten()):
                tag_id = int(tag_id)
                pts = corner.reshape(4, 2)
                side_px = float(np.linalg.norm(pts[0] - pts[1]))
                if side_px < 4:  # too small/far to be meaningful
                    continue
                tag_cx = float(pts[:, 0].mean())
                # Small-angle distance model: size shrinks with 2*tan(fov/2)
                distance = None
                bearing = None
                if side_px > 0:
                    image_fraction = side_px / w
                    try:
                        distance = TAG_SIZE_M / (
                            image_fraction * 2.0 * math.tan(math.radians(CAM_HFOV_DEG) / 2.0))
                    except ZeroDivisionError:
                        distance = None
                    bearing = math.degrees(math.atan2(
                        (tag_cx - cx) / cx, 1.0 / math.tan(math.radians(CAM_HFOV_DEG) / 2.0)))
                det = {
                    "tag_id": tag_id,
                    "timestamp": now,
                    "frame_id": frame_id,
                    "source": source,
                    "distance_m": round(distance, 3) if distance else None,
                    "bearing_deg": round(bearing, 1) if bearing is not None else None,
                    "orientation_deg": None,  # pose needs calibrated intrinsics
                    "confidence": min(1.0, side_px / (w * 0.2)),
                    "image_w": w,
                    "image_h": h,
                    "corners": pts.tolist(),
                }
                results.append(det)
    except Exception as exc:
        logger.error("AprilTag detection error: %s", exc)
        return []

    with _lock:
        _last_frame_at = now
        _frames_seen += 1
        for det in results:
            _history.append(det)
            _last_by_tag[det["tag_id"]] = det
        _frame_results.append({"frame_id": frame_id, "timestamp": now,
                               "results": results})

    # Feed each detection into the stability-filtered localization service.
    # This is non-throwing so a localization error never kills the vision pipe.
    if results:
        try:
            from services import localization_service
            best = max(results, key=lambda d: d.get("confidence") or 0)
            localization_service.update(best)
        except Exception as _loc_exc:
            logger.debug("localization update error: %s", _loc_exc)

    return results


def get_detections_for_frame(frame_id: str) -> list[dict]:
    """Detections found in a specific frame (Phase 6 mapping correlation).
    Empty list for unknown/expired frame ids."""
    with _lock:
        for entry in reversed(_frame_results):
            if entry["frame_id"] == frame_id:
                return list(entry["results"])
    return []


def inject_detection(tag_id: int, distance_m: float | None = None,
                     bearing_deg: float | None = None) -> dict:
    """Simulation/testing hook: push a synthetic detection through the same
    bookkeeping as a camera detection (history, last-seen, localization)."""
    now = time.time()
    det = {
        "tag_id": int(tag_id),
        "timestamp": now,
        "frame_id": "sim",
        "source": "simulation",
        "distance_m": distance_m,
        "bearing_deg": bearing_deg,
        "orientation_deg": None,
        "confidence": 1.0,
        "image_w": None,
        "image_h": None,
        "corners": None,
        "simulated": True,
    }
    with _lock:
        _history.append(det)
        _last_by_tag[det["tag_id"]] = det
    # Feed injected simulation detections into localization filter too
    try:
        from services import localization_service
        localization_service.update(det)
    except Exception:
        pass
    return det


def set_detection_enabled(enabled: bool) -> None:
    global _detections_enabled
    _detections_enabled = bool(enabled)


def get_recent(limit: int = 20) -> list[dict]:
    with _lock:
        items = list(_history)[-limit:]
    return items[::-1]  # newest first


def get_visible(max_age_s: float = DETACH_OLDER_THAN_S) -> list[dict]:
    """Tags seen within the freshness window, deduplicated to the latest per id."""
    now = time.time()
    with _lock:
        return [dict(det) for det in _last_by_tag.values()
                if now - det["timestamp"] <= max_age_s]


def get_last_seen(tag_id: int) -> Optional[dict]:
    with _lock:
        det = _last_by_tag.get(int(tag_id))
        return dict(det) if det else None


def stats() -> dict:
    with _lock:
        return {
            "enabled": APRILTAG_ENABLED and _detections_enabled,
            "opencv_available": _CV_OK,
            "simulated": _simulated,
            "family": FAMILY_NAME,
            "frames_seen": _frames_seen,
            "last_frame_age_s": round(time.time() - _last_frame_at, 2) if _last_frame_at else None,
            "unique_tags_seen": len(_last_by_tag),
            "history_size": len(_history),
        }
