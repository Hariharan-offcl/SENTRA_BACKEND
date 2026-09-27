"""
SENTRA — Manual mapping service (Phase 6).

Mapping sessions overlay MANUAL driving: the user drives the rover with the
Flutter joystick; whenever the phone camera sees an AprilTag during an active
session, the backend captures:

    tag_id, timestamp, camera frame (JPEG snapshot), IMU angles,
    encoder odometry, ultrasonic distances, detection geometry

as a *pending capture*. The user then names it via POST /map/tag, which
writes the binding into the persistent tag map (Phase 5). Unnamed captures
expire after CAPTURE_TTL_S to keep the session clean.

Session state and captures are in-memory; the durable artifact is the tag
map itself (and snapshot files on disk).

Env vars:
    SENTRA_MAPPING_ENABLED    (default true)
    SENTRA_MAP_SNAPSHOT_DIR   (default ~/sentra_maps)
    SENTRA_CAPTURE_TTL_S      (default 600 — 10 min to name a capture)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional

from core.state import get_robot_state, MANUAL

logger = logging.getLogger(__name__)

MAPPING_ENABLED = os.getenv("SENTRA_MAPPING_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
SNAPSHOT_DIR = os.path.expanduser(
    os.getenv("SENTRA_MAP_SNAPSHOT_DIR", "~/sentra_maps"))
CAPTURE_TTL_S = float(os.getenv("SENTRA_CAPTURE_TTL_S", "600"))
CAPTURES_MAX = 50

_lock = threading.RLock()
_session: Optional[dict] = None
_session_seq = 0


def _rover_context() -> dict:
    """Robot context snapshot at capture time (IMU + encoders + ultrasonic)."""
    try:
        from services import sensor_service
        snap = sensor_service.get_snapshot()
        return {
            "imu": snap.get("imu", {}),
            "wheel_encoders": snap.get("wheel_encoders", {}),
            "front_distance_m": snap.get("front_distance_m"),
            "rear_distance_m": snap.get("rear_distance_m"),
        }
    except Exception:
        return {}


def _save_frame_jpeg(frame, capture_id: str) -> Optional[str]:
    """Persist the frame the tag was seen in. Returns path or None."""
    try:
        import cv2
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOT_DIR, f"map_{capture_id}.jpg")
        cv2.imwrite(path, frame)
        return path
    except Exception as exc:
        logger.warning("Map snapshot save failed: %s", exc)
        return None


# ── Vision consumer (registered in main.py) ─────────────────────────────────

def on_frame(frame, frame_id: str) -> None:
    """Vision subscriber: capture any tags seen while a session is active.
    Normally the apriltag subscriber processed this frame_id first; if not
    (subscriber ordering), fall back to detecting here so no tag is missed."""
    if _session is None:
        return
    from services import apriltag_service
    detections = apriltag_service.get_detections_for_frame(frame_id)
    if not detections and frame is not None:
        detections = apriltag_service.process_frame(frame, frame_id)
    if not detections:
        return
    for det in detections:
        capture_tag_detection(det, frame)


def capture_tag_detection(det: dict, frame=None) -> Optional[dict]:
    """Turn one detection into a pending capture (dedup per tag within TTL)."""
    global _session_seq
    if not MAPPING_ENABLED:
        return None
    now = time.time()
    with _lock:
        if _session is None:
            return None
        # Dedup: same tag already captured and not yet named → refresh it
        for cap in _session["captures"]:
            if cap["tag_id"] == det["tag_id"] and not cap["named"]:
                cap["seen_count"] += 1
                cap["last_seen_at"] = now
                cap["detection"] = dict(det)
                return cap

        _session_seq += 1
        capture_id = f"{_session['id']}-c{_session_seq}"
        capture = {
            "capture_id": capture_id,
            "tag_id": det["tag_id"],
            "named": False,
            "name": None,
            "seen_count": 1,
            "first_seen_at": now,
            "last_seen_at": now,
            "detection": {k: det.get(k) for k in (
                "distance_m", "bearing_deg", "confidence", "source")},
            "context": _rover_context(),
            "frame_path": _save_frame_jpeg(frame, str(capture_id)) if frame is not None else None,
        }
        _session["captures"].append(capture)
        # Bound the list (oldest unnamed first)
        if len(_session["captures"]) > CAPTURES_MAX:
            _session["captures"].pop(0)
        logger.info("Mapping capture %s: tag %d", capture_id, det["tag_id"])
        return dict(capture)


# ── Session lifecycle ────────────────────────────────────────────────────────

def start_session(started_by: str = "app") -> dict:
    """Start a mapping session. Requires MANUAL mode (user drives while mapping)."""
    global _session
    st = get_robot_state()
    if st.is_estop_active():
        return {"ok": False, "error": "estop_latched"}
    if st.get_mode() != MANUAL:
        result = st.request_mode(MANUAL, requested_by="mapping_service")
        if not result.get("accepted"):
            return {"ok": False, "error": f"mode_not_available ({st.get_mode()})"}
    with _lock:
        if _session is not None and _session["active"]:
            return {"ok": True, "session": dict(_session), "already_active": True}
        _session = {
            "id": f"map-{int(time.time())}",
            "active": True,
            "started_by": started_by,
            "started_at": time.time(),
            "captures": [],
        }
        logger.info("Mapping session %s started by %s", _session["id"], started_by)
        return {"ok": True, "session": dict(_session)}


def stop_session() -> dict:
    """Stop the active session. Named captures already persist via tag_map."""
    global _session
    with _lock:
        if _session is None:
            return {"ok": True, "session": None, "already_stopped": True}
        _session["active"] = False
        _session["stopped_at"] = time.time()
        summary = dict(_session)
        _session = None
    logger.info("Mapping session stopped: %s", summary.get("id"))
    return {"ok": True, "session": summary}


def get_session() -> Optional[dict]:
    with _lock:
        return dict(_session) if _session else None


def _cleanup_expired_locked() -> None:
    """Caller must hold _lock. Expires unnamed captures past TTL."""
    if _session is None:
        return
    now = time.time()
    _session["captures"] = [
        c for c in _session["captures"]
        if c["named"] or (now - c["last_seen_at"]) <= CAPTURE_TTL_S
    ]


def list_captures(include_named: bool = True) -> list[dict]:
    with _lock:
        _cleanup_expired_locked()
        if _session is None:
            return []
        caps = [dict(c) for c in _session["captures"] if include_named or not c["named"]]
    caps.sort(key=lambda c: c["first_seen_at"], reverse=True)
    return caps


def get_capture(capture_id: str) -> Optional[dict]:
    with _lock:
        if _session is None:
            return None
        for c in _session["captures"]:
            if c["capture_id"] == capture_id:
                return dict(c)
    return None


def name_capture(tag_id: int, name: str, type: str = "LOCATION",
                 notes: str = "") -> dict:
    """
    Assign a name to a pending capture for `tag_id` (or create the binding
    directly if no capture exists — the user may already know the tag id).
    Persists via services.tag_map. Returns {'ok': ..., ...}.
    """
    from services import tag_map
    with _lock:
        target = None
        if _session is not None:
            _cleanup_expired_locked()
            for c in _session["captures"]:
                if c["tag_id"] == int(tag_id) and not c["named"]:
                    target = c
                    break
    try:
        entry = tag_map.upsert_tag(tag_id, name, type, notes)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if target is not None:
        with _lock:
            target["named"] = True
            target["name"] = entry["name"]
            target["named_at"] = time.time()

    return {
        "ok": True,
        "tag": entry,
        "named_from_capture": target is not None,
        "capture_id": target["capture_id"] if target else None,
    }


def delete_capture(capture_id: str) -> bool:
    """Discard a pending (unnamed) capture; optionally delete its frame file."""
    with _lock:
        if _session is None:
            return False
        for i, c in enumerate(_session["captures"]):
            if c["capture_id"] == capture_id:
                if not c["named"]:
                    _session["captures"].pop(i)
                    frame_path = c.get("frame_path")
                    if frame_path and os.path.exists(frame_path):
                        try:
                            os.remove(frame_path)
                        except OSError:
                            pass
                    return True
                return False  # named captures are part of the record
    return False


def stats() -> dict:
    with _lock:
        sess = dict(_session) if _session else None
    if sess:
        sess["unnamed_count"] = sum(1 for c in sess["captures"] if not c["named"])
        sess["named_count"] = sum(1 for c in sess["captures"] if c["named"])
    return {
        "enabled": MAPPING_ENABLED,
        "active": _session is not None,
        "session": sess,
        "ttl_s": CAPTURE_TTL_S,
        "snapshot_dir": SNAPSHOT_DIR,
    }
