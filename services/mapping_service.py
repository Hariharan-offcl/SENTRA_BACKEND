"""
SENTRA — Manual mapping service (Phase 6, Phase 0 repair).

The user drives the robot in MANUAL mode; AprilTag sightings become named
locations in the persistent tag map, while a 10 Hz odometry trace is written
to disk for the session.

Phase 0 repair: this module previously exposed an instance-based API
(`mapping_service.start_session()` on a MappingService singleton) that matched
neither its tests, nor the router, nor the pydantic models — and read the
wrong telemetry battery keys. It is now the module-level API everything else
already expected:

    start_session(started_by=...)         → {"ok", "session"} / already_active / error
    stop_session()                        → {"ok"} / already_stopped
    get_session()                         → session dict or None (active only)
    capture_tag_detection(det)            → capture dict or None (dedup + TTL)
    list_captures(named=None)             → [capture dict]
    get_capture(capture_id)               → capture dict or None
    name_capture(tag_id, name, type, notes) → {"ok", "tag", "named_from_capture", ...}
    delete_capture(capture_id)            → bool (named captures protected)
    update_odometry(d_dist, d_heading)    — integrate dead-reckoning pose
    on_frame(frame, frame_id)             — vision-hub subscriber (captures tags)
    recording_loop()                      — async 10 Hz trace writer (lifespan)

Module vars tests/ops may re-point: SNAPSHOT_DIR, CAPTURE_TTL_S.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Optional

from core import config as core_config
from core.state import MANUAL, get_robot_state

logger = logging.getLogger(__name__)

# Mapping session traces (JSONL) are written here. Re-pointable in tests.
SNAPSHOT_DIR: str = core_config.SENTRA_MAP_SNAPSHOT_DIR
# Unnamed captures expire after this long without a re-sighting.
CAPTURE_TTL_S: float = 600.0


@dataclass
class MappingPoint:
    timestamp: float
    x: float
    y: float
    heading: float
    tag_id: Optional[int]
    room_name: Optional[str]
    battery_pct: float
    current_ma: float
    health: str


# ── Session state (module-level, guarded by _lock) ───────────────────────────
_lock = threading.Lock()
_session: Optional[dict] = None
_file_handle = None

# Dead-reckoning pose relative to the session origin (0, 0, 0)
_x = 0.0
_y = 0.0
_heading = 0.0

# Freshest tag seen this session (set by on_frame / capture_tag_detection)
_last_tag_id: Optional[int] = None
_last_room: Optional[str] = None

# Small ring buffer kept for future burst writes
_buffer: deque = deque(maxlen=200)


def _now() -> float:
    return time.time()


def _session_snapshot(sess: Optional[dict]) -> Optional[dict]:
    if sess is None:
        return None
    return {
        "id": sess["id"],
        "active": sess["active"],
        "started_by": sess["started_by"],
        "started_at": sess["started_at"],
        "captures": [dict(c) for c in sess["captures"]],
        "stopped_at": sess.get("stopped_at"),
    }


# ── Session lifecycle ────────────────────────────────────────────────────────

def start_session(started_by: str = "user") -> dict:
    """Start a mapping session. Adopts MANUAL mode; refuses during e-stop."""
    global _session, _file_handle, _x, _y, _heading, _last_tag_id, _last_room

    st = get_robot_state()
    if st.is_estop_active():
        return {"ok": False, "error": "estop active — reset it before mapping"}
    if st.get_mode() != MANUAL:
        result = st.request_mode(MANUAL, requested_by="mapping_service")
        if not result.get("accepted"):
            return {"ok": False, "error": "could not enter MANUAL mode"}

    with _lock:
        if _session is not None and _session["active"]:
            return {"ok": True, "session": _session_snapshot(_session),
                    "already_active": True}

        try:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            sid = f"session_{int(time.time())}"
            path = os.path.join(SNAPSHOT_DIR, f"{sid}_trace.jsonl")
            _file_handle = open(path, "a", encoding="utf-8")
        except OSError as exc:
            logger.error("Mapping trace file unavailable: %s", exc)
            return {"ok": False, "error": f"cannot open trace file: {exc}"}

        _x = 0.0
        _y = 0.0
        _heading = 0.0
        _last_tag_id = None
        _last_room = None
        _session = {
            "id": sid, "active": True, "started_by": started_by,
            "started_at": _now(), "captures": [], "stopped_at": None,
        }
        logger.info("Mapping session %s started by %s (origin 0,0,0)",
                    sid, started_by)
        return {"ok": True, "session": _session_snapshot(_session)}


def stop_session() -> dict:
    """Stop the active session (clears it — get_session() returns None after)."""
    global _session, _file_handle
    with _lock:
        if _session is None or not _session["active"]:
            return {"ok": True, "already_stopped": True}
        sid = _session["id"]
        _session = None
        if _file_handle:
            try:
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
        logger.info("Mapping session %s stopped", sid)
        return {"ok": True}


def get_session() -> Optional[dict]:
    """Snapshot of the active session, or None."""
    with _lock:
        return _session_snapshot(_session)


# ── Captures ─────────────────────────────────────────────────────────────────

def _detection_model(det: dict) -> dict:
    return {
        "distance_m": det.get("distance_m"),
        "bearing_deg": det.get("bearing_deg"),
        "confidence": det.get("confidence"),
        "source": det.get("source", "phone"),
    }


def _context_model() -> dict:
    try:
        from services import sensor_service
        snap = sensor_service.get_snapshot()
    except Exception:
        snap = {}
    try:
        from services.telemetry_service import _US_SIM
        front = _US_SIM.get("front_distance_m")
        rear  = _US_SIM.get("rear_distance_m")
    except Exception:
        front = snap.get("front_distance_m")
        rear  = snap.get("rear_distance_m")
    return {
        "imu":            snap.get("imu", {}) or {},
        "front_distance_m": front,
        "rear_distance_m":  rear,
    }


def _purge_expired_locked(now: float) -> None:
    """Drop unnamed captures past their TTL. Caller holds _lock."""
    if _session is None:
        return
    _session["captures"] = [
        c for c in _session["captures"]
        if c["named"] or now - c["last_seen_at"] <= CAPTURE_TTL_S
    ]


def capture_tag_detection(det: dict) -> Optional[dict]:
    """Record one AprilTag sighting as a capture (deduped while unnamed)."""
    tag_id = det.get("tag_id")
    if tag_id is None:
        return None
    now = _now()
    with _lock:
        if _session is None or not _session["active"]:
            return None
        _purge_expired_locked(now)
        for cap in _session["captures"]:
            if cap["tag_id"] == int(tag_id) and not cap["named"]:
                cap["seen_count"] += 1
                cap["last_seen_at"] = now
                cap["detection"] = _detection_model(det)
                return dict(cap)
        cap = {
            "capture_id": f"cap-{int(now * 1000):x}-{len(_session['captures']) + 1}",
            "tag_id": int(tag_id),
            "named": False,
            "name": None,
            "seen_count": 1,
            "first_seen_at": now,
            "last_seen_at": now,
            "detection": _detection_model(det),
            "context": _context_model(),
            "frame_path": None,
        }
        _session["captures"].append(cap)
        return dict(cap)


def list_captures(named: Optional[bool] = None) -> list[dict]:
    """Captures of the active session (expired unnamed ones purged)."""
    with _lock:
        if _session is None:
            return []
        _purge_expired_locked(_now())
        caps = [dict(c) for c in _session["captures"]]
    if named is not None:
        caps = [c for c in caps if c["named"] == named]
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
    """Name a tag (binding it to a pending capture when one exists)."""
    from services import tag_map
    try:
        tag = tag_map.upsert_tag(tag_id, name, type, notes)
    except ValueError as exc:
        return {"ok": False, "tag": {}, "error": str(exc)}
    with _lock:
        if _session is not None:
            _purge_expired_locked(_now())
            for c in _session["captures"]:
                if c["tag_id"] == int(tag_id) and not c["named"]:
                    c["named"] = True
                    c["name"] = name.strip()
                    return {"ok": True, "tag": tag,
                            "named_from_capture": True,
                            "capture_id": c["capture_id"]}
    return {"ok": True, "tag": tag, "named_from_capture": False,
            "capture_id": None}


def delete_capture(capture_id: str) -> bool:
    """Discard an unnamed capture; named captures are protected."""
    with _lock:
        if _session is None:
            return False
        for i, c in enumerate(_session["captures"]):
            if c["capture_id"] == capture_id:
                if c["named"]:
                    return False
                del _session["captures"][i]
                return True
    return False


# ── Odometry + vision correlation ────────────────────────────────────────────

def update_odometry(delta_dist: float, delta_heading: float) -> None:
    """Integrate wheel distance and IMU yaw into the session pose."""
    global _x, _y, _heading
    with _lock:
        if _session is None or not _session["active"]:
            return
        rad = math.radians(_heading)
        _x += delta_dist * math.cos(rad)
        _y += delta_dist * math.sin(rad)
        _heading = (_heading + delta_heading) % 360.0


def on_frame(frame, frame_id: str = "") -> None:
    """Vision-hub subscriber (wired in main.py): turn fresh AprilTag
    detections into captures while a session is active. Non-throwing — a bad
    frame must never take down the vision pipeline."""
    try:
        from services import apriltag_service, tag_map
        dets = apriltag_service.get_detections_for_frame(frame_id)
        if not dets:
            return
        best = max(dets, key=lambda d: d.get("confidence") or 0)
        tag_id = best.get("tag_id")
        if tag_id is None:
            return
        capture_tag_detection({
            "tag_id": tag_id,
            "distance_m": best.get("distance_m"),
            "bearing_deg": best.get("bearing_deg"),
            "confidence": best.get("confidence"),
            "source": best.get("source", "phone"),
        })
        tag = tag_map.get_tag(int(tag_id))
        global _last_tag_id, _last_room
        with _lock:
            _last_tag_id = int(tag_id)
            _last_room = (tag or {}).get("name")
    except Exception as exc:  # never break the vision pipeline
        logger.debug("mapping on_frame error: %s", exc)


# ── Trace recording (async, started from lifespan) ───────────────────────────

def _telemetry() -> tuple[float, float, str]:
    """(battery_pct, current_ma, health) — pulled from real service layer."""
    try:
        from services import current_service
        prov = current_service.sensor_provider()
        curr_data = prov.get("current", {})
        current_ma = float(curr_data.get("current", 0.0) or 0.0)
        # We don't have a battery gauge sensor; report 0 until one is wired.
        battery = 0.0
        health = "OK" if current_ma < 3000 else "OVERCURRENT"
        return battery, current_ma, health
    except Exception:
        return 0.0, 0.0, "UNKNOWN"


async def recording_loop() -> None:
    """Background loop capturing 10 Hz trace points while a session is active."""
    while True:
        try:
            with _lock:
                active = _session is not None and _session["active"]
                fh = _file_handle
                x, y, heading = _x, _y, _heading
                tag_id, room = _last_tag_id, _last_room
            if active and fh is not None:
                battery, current, health = _telemetry()
                point = MappingPoint(
                    timestamp=_now(), x=x, y=y, heading=heading,
                    tag_id=tag_id, room_name=room or "unknown",
                    battery_pct=battery, current_ma=current, health=health,
                )
                fh.write(json.dumps(asdict(point)) + "\n")
                fh.flush()
        except Exception as exc:
            logger.error("Mapping record error: %s", exc)
        await asyncio.sleep(0.1)  # 10 Hz


def get_current_position() -> dict:
    """Current relative pose (+ whether a session is active)."""
    with _lock:
        return {
            "x": _x,
            "y": _y,
            "heading": _heading,
            "active": _session is not None and _session["active"],
        }
