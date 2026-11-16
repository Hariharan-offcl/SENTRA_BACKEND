"""
SENTRA – Localization service (Phase 5, hardened).

Answers:
    - Where am I?            → last known location + confidence + age
    - Which tag is visible?  → currently-visible tags (freshness window)
    - What location is it?   → tag map lookup

Stability filter:
    A tag must be seen CONFIRM_HITS times within CONFIRM_WINDOW_S before it
    is accepted as the new confirmed location.  This prevents a single noisy
    frame from flipping the robot's claimed position.

    The 'last_known' field always reflects the CONFIRMED location.
    The 'candidate' field (if present) shows what is being considered.

Tag-loss handling:
    When no tag has been seen for STALE_S seconds the service marks the
    location as STALE in telemetry but does NOT clear last_known — the
    robot must still know where it was.

Event hook:
    Register a callback with on_location_change(fn) to be notified
    synchronously whenever the confirmed location changes.  The telemetry
    assembler and navigation service wire into this.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from services import tag_map, apriltag_service

logger = logging.getLogger(__name__)

# ── Stability parameters ────────────────────────────────────────────────────
# A tag must be seen this many times in the window before location changes.
CONFIRM_HITS    = 3
CONFIRM_WINDOW_S = 2.0   # seconds
STALE_S         = 10.0   # seconds without sighting → location considered stale

_lock = threading.RLock()

# Confirmed state
_last_known: Optional[dict] = None   # last CONFIRMED location description
_confirmed_at: float = 0.0           # when it was confirmed
_last_seen_any_at: float = 0.0       # last time any registered tag was visible

# Candidate (pre-confirmation) state
_candidate_tag_id: Optional[int] = None
_candidate_hits: int = 0
_candidate_first_hit: float = 0.0
_candidate_det: Optional[dict] = None

# Observers
_change_callbacks: list[Callable[[dict], None]] = []


# ── Observer API ──────────────────────────────────────────────────────────────

def on_location_change(fn: Callable[[dict], None]) -> None:
    """Register fn(location_dict) called on every confirmed location change."""
    with _lock:
        if fn not in _change_callbacks:
            _change_callbacks.append(fn)


def _fire_callbacks(loc: dict) -> None:
    """Call all registered observers (caller must NOT hold _lock)."""
    for fn in list(_change_callbacks):
        try:
            fn(loc)
        except Exception as exc:
            logger.error("localization callback error: %s", exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _describe(det: dict) -> Optional[dict]:
    """Turn a raw apriltag detection dict into a location description.
    Returns None for unregistered tags."""
    tag = tag_map.describe_tag(det["tag_id"])
    if tag is None:
        return None
    return {
        "tag_id":     det["tag_id"],
        "name":       tag["name"],
        "type":       tag["type"],
        "registered": True,
        "seen_at":    det["timestamp"],
        "age_s":      round(time.time() - det["timestamp"], 2),
        "distance_m": det.get("distance_m"),
        "bearing_deg": det.get("bearing_deg"),
        "confidence": round(det.get("confidence") or 0.0, 3),
        "source":     det.get("source", "phone"),
    }


def _best_visible() -> Optional[dict]:
    """Return the highest-quality registered visible tag description."""
    visible  = apriltag_service.get_visible()
    described = [d for d in (_describe(det) for det in visible) if d is not None]
    if not described:
        return None
    described.sort(key=lambda d: (
        0 if d["type"] == "DOCK" else 1,
        d["distance_m"] if d["distance_m"] is not None else 999.0,
        -d["confidence"],
    ))
    return described[0]


# ── Main update (called on each AprilTag detection + by telemetry assembler) ──

def update(det: Optional[dict] = None) -> None:
    """Advance the stability filter with the latest best visible tag.

    det – if supplied, a single fresh raw detection dict (from apriltag hook);
          otherwise the service polls apriltag_service itself.
    """
    global _last_known, _confirmed_at, _last_seen_any_at
    global _candidate_tag_id, _candidate_hits, _candidate_first_hit, _candidate_det

    now = time.time()
    best = _describe(det) if det else _best_visible()

    if best is None:
        # No registered tag in view – leave last_known intact
        return

    with _lock:
        _last_seen_any_at = now
        tag_id = best["tag_id"]

        # ── Same location already confirmed: update freshness, reset candidate ──
        if _last_known and _last_known["tag_id"] == tag_id:
            _last_known = best          # refresh distance / bearing / age
            _confirmed_at = now
            _candidate_tag_id = None
            _candidate_hits   = 0
            return

        # ── New candidate accumulation ───────────────────────────────────────
        if _candidate_tag_id != tag_id:
            # Switch candidate
            _candidate_tag_id   = tag_id
            _candidate_hits     = 1
            _candidate_first_hit = now
            _candidate_det      = best
        else:
            _candidate_hits += 1
            _candidate_det   = best     # keep freshest

        # ── Confirm when we have enough hits inside the window ───────────────
        window_ok = (now - _candidate_first_hit) <= CONFIRM_WINDOW_S
        if _candidate_hits >= CONFIRM_HITS and window_ok:
            old_name = _last_known["name"] if _last_known else None
            _last_known         = _candidate_det
            _confirmed_at       = now
            _candidate_tag_id   = None
            _candidate_hits     = 0
            new_loc = dict(_last_known)

        elif not window_ok:
            # Window expired – restart with this sighting as first hit
            _candidate_hits      = 1
            _candidate_first_hit = now
            _candidate_det       = best
            return
        else:
            return

    # Outside lock: fire callbacks
    logger.info("Localization: confirmed location '%s' (tag %d, %.2f m)",
                new_loc.get("name"), new_loc.get("tag_id"),
                new_loc.get("distance_m") or 0.0)
    if new_loc.get("name") != old_name:
        _fire_callbacks(new_loc)


# ── Public API ────────────────────────────────────────────────────────────────

def get_localization() -> dict:
    """Full localization status snapshot (safe to call any time)."""
    # Poll latest visible tags to keep state fresh
    update()
    now = time.time()
    with _lock:
        last = dict(_last_known) if _last_known else None
        confirmed_at  = _confirmed_at
        last_seen_any = _last_seen_any_at
        cand_id       = _candidate_tag_id
        cand_hits     = _candidate_hits

    age_s = round(now - last_seen_any, 1) if last_seen_any else None
    stale = age_s is not None and age_s > STALE_S

    return {
        "located":             last is not None,
        "last_known":          last,
        "confirmed_at":        confirmed_at if confirmed_at else None,
        "stale":               stale,
        "tag_age_s":           age_s,
        "visible_tags":        _visible_described(),
        "localized_recently":  bool(last and (now - (last.get("seen_at") or 0)) <= STALE_S),
        # Candidate info (useful for debugging / mapping UI)
        "candidate": {
            "tag_id": cand_id,
            "hits":   cand_hits,
            "needed": CONFIRM_HITS,
        } if cand_id is not None else None,
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


def current_location_full() -> Optional[dict]:
    """Full location dict for navigation/telemetry, or None."""
    with _lock:
        return dict(_last_known) if _last_known else None


def current_zone_for_telemetry() -> str:
    """Location name string for the telemetry payload – 'Unknown' when not set."""
    name = current_location()
    return name if name else "Unknown"


def is_at(location_name: str) -> bool:
    """True when the confirmed location matches location_name (case-insensitive)."""
    with _lock:
        if _last_known is None:
            return False
        return _last_known["name"].strip().lower() == location_name.strip().lower()


def is_stale() -> bool:
    """True when the last confirmed tag was seen more than STALE_S seconds ago."""
    with _lock:
        if not _last_seen_any_at:
            return True
        return (time.time() - _last_seen_any_at) > STALE_S


def reset() -> None:
    """Clear all localization state (e.g. on session reset)."""
    global _last_known, _confirmed_at, _last_seen_any_at
    global _candidate_tag_id, _candidate_hits, _candidate_first_hit, _candidate_det
    with _lock:
        _last_known       = None
        _confirmed_at     = 0.0
        _last_seen_any_at = 0.0
        _candidate_tag_id = None
        _candidate_hits   = 0
        _candidate_first_hit = 0.0
        _candidate_det    = None
    logger.info("Localization reset")
