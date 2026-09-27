"""
SENTRA — Localization service (Phase 5).

Answers the Phase 5 questions:
    - Where am I?            → last known location + confidence + age
    - Which tag is visible?  → currently-visible tags (freshness window)
    - What location is it?   → tag map lookup

Zone = the tag's mapped name. When several tags are visible, the closest
(smallest distance / highest confidence) wins. When none are visible, the
'last known' entry persists with a growing staleness age.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from services import tag_map, apriltag_service

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_known: Optional[dict] = None


def _describe(det: dict) -> Optional[dict]:
    tag = tag_map.describe_tag(det["tag_id"])
    if tag is None:
        return None
    return {
        "tag_id": det["tag_id"],
        "name": tag["name"],
        "type": tag["type"],
        "registered": True,
        "seen_at": det["timestamp"],
        "age_s": round(time.time() - det["timestamp"], 2),
        "distance_m": det.get("distance_m"),
        "bearing_deg": det.get("bearing_deg"),
        "confidence": round(det.get("confidence") or 0.0, 3),
        "source": det.get("source", "phone"),
    }


def update() -> None:
    """Fold the latest detections into last-known state (idempotent)."""
    global _last_known
    visible = apriltag_service.get_visible()
    described = [d for d in (_describe(det) for det in visible) if d is not None]
    if not described:
        return
    # Prefer DOCK tags at similar confidence, then closest, then most confident.
    described.sort(key=lambda d: (
        0 if d["type"] == "DOCK" else 1,
        d["distance_m"] if d["distance_m"] is not None else 999.0,
        -d["confidence"],
    ))
    best = described[0]
    with _lock:
        if _last_known is None or best["seen_at"] >= _last_known["seen_at"]:
            _last_known = best


def get_localization() -> dict:
    """Full localization status."""
    update()
    with _lock:
        last = dict(_last_known) if _last_known else None
    return {
        "located": last is not None,
        "last_known": last,
        "visible_tags": _visible_described(),
        "localized_recently": bool(last and (time.time() - last["seen_at"]) <= 10.0),
    }


def _visible_described() -> list[dict]:
    visible = apriltag_service.get_visible()
    out = [d for d in (_describe(det) for det in visible) if d is not None]
    out.sort(key=lambda d: (
        0 if d["type"] == "DOCK" else 1,
        d["distance_m"] if d["distance_m"] is not None else 999.0,
        -d["confidence"],
    ))
    return out


def current_location() -> Optional[str]:
    update()
    with _lock:
        return _last_known["name"] if _last_known else None


def current_zone_for_telemetry() -> str:
    """'ZONE' string for the existing telemetry payload — real when known."""
    name = current_location()
    return name if name else "Unknown"


def reset() -> None:
    global _last_known
    with _lock:
        _last_known = None
