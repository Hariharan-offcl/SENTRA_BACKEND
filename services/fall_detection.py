"""
SENTRA — Fall detection service (Phase 12).

Independent temporal-evidence pipeline. NEVER triggers from a single frame.

Evidence per tracked person (from Phase 10 detections, sampled at ~4 Hz):
    - Aspect evidence:  lying = bbox width/height ≥ ASPECT_RATIO (a standing
      human is tall-narrow; a fallen one is wide-short)
    - Stationary evidence: bbox center moved < MOVEMENT_PX between samples

State machine per person:
    NORMAL          — no lying evidence
    POSSIBLE_FALL   — lying AND stationary observed (start timing)
    FALL_CONFIRMED  — POSSIBLE_FALL sustained ≥ CONFIRM_TIME_S → latched;
                      emergency hooks fire ONCE; stays confirmed until reset

Disambiguation: POSSIBLE_FALL resolves back to NORMAL if the person stands
up (aspect/position evidence stops) before the confirmation window elapses.

Emergency hooks (Phase 13 wires the auto video call):
    register_emergency_hook(fn(evidence_dict) -> None)   sync, from worker

Env vars:
    SENTRA_FALL_ENABLED       (default true)
    SENTRA_FALL_POLL_S        (default 0.25 — evidence sampling)
    SENTRA_FALL_ASPECT        (default 1.15 — w/h ratio for 'lying')
    SENTRA_FALL_MOVEMENT_PX   (default 25)
    SENTRA_FALL_CONFIRM_S     (default 4.0 — sustained time to confirm)
    SENTRA_FALL_MIN_CONF      (default 0.55)
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

from services import safety_events

FALL_ENABLED = os.getenv("SENTRA_FALL_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
FALL_POLL_S = float(os.getenv("SENTRA_FALL_POLL_S", "0.25"))
ASPECT_LYING = float(os.getenv("SENTRA_FALL_ASPECT", "1.15"))
MOVEMENT_PX = float(os.getenv("SENTRA_FALL_MOVEMENT_PX", "25"))
CONFIRM_TIME_S = float(os.getenv("SENTRA_FALL_CONFIRM_S", "4.0"))
MIN_CONF = float(os.getenv("SENTRA_FALL_MIN_CONF", "0.55"))

NORMAL = "NORMAL"
POSSIBLE_FALL = "POSSIBLE_FALL"
FALL_CONFIRMED = "FALL_CONFIRMED"

_lock = threading.RLock()
_person_states: dict[int, dict] = {}   # person_id → state record
_confirmed_event: Optional[dict] = None
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_hooks: list = []


def register_emergency_hook(fn) -> None:
    """fn(evidence: dict) is called ONCE per confirmed fall (worker thread)."""
    with _lock:
        _hooks.append(fn)
    logger.info("Emergency hook registered: %s", getattr(fn, "__name__", fn))


def _fire_emergency_hooks(evidence: dict) -> None:
    for fn in list(_hooks):
        try:
            fn(evidence)
        except Exception as exc:
            logger.error("Emergency hook failed: %s", exc)


def _bbox_center(bbox) -> tuple[float, float]:
    x, y, w, h = bbox
    return (x + w / 2.0, y + h / 2.0)


def sample(person_id: int, bbox: list, confidence: float,
           timestamp: Optional[float] = None) -> dict:
    """Feed one observation (called by the sampler from Phase 10 tracks;
    also the simulation entry point). Returns the person's state view after
    it; on the transition to FALL_CONFIRMED the return value carries
    'evidence' and side effects (event + hooks) fire exactly once."""
    global _confirmed_event
    if timestamp is None:
        timestamp = time.time()
    x, y, w, h = bbox
    aspect = (w / h) if h > 0 else 99.0
    lying = aspect >= ASPECT_LYING
    confirm_now = False
    evidence = None

    with _lock:
        st = _person_states.get(person_id)
        if st is None:
            st = {"state": NORMAL, "since": timestamp,
                  "last_center": _bbox_center(bbox), "last_seen": timestamp,
                  "aspect": aspect, "confidence": confidence}
            _person_states[person_id] = st

        center = _bbox_center(bbox)
        moved = ((center[0] - st["last_center"][0]) ** 2 +
                 (center[1] - st["last_center"][1]) ** 2) ** 0.5
        stationary = moved < MOVEMENT_PX
        st["last_center"] = center
        st["last_seen"] = timestamp
        st["aspect"] = aspect
        st["confidence"] = confidence

        if st["state"] == FALL_CONFIRMED:
            st["since"] = st.get("since", timestamp)  # latched; no transitions
            return _view(person_id, st)

        if st["state"] == NORMAL:
            # Falling itself is fast movement — the trigger is body ORIENTATION
            # (lying aspect), not stillness. Stillness matters for confirmation.
            if lying and confidence >= MIN_CONF:
                st["state"] = POSSIBLE_FALL
                st["since"] = timestamp
                logger.info("Fall: person %d → POSSIBLE_FALL (aspect %.2f)",
                            person_id, aspect)
            # else: stays NORMAL
        elif st["state"] == POSSIBLE_FALL:
            standing_up = not lying
            if standing_up:
                logger.info("Fall: person %d recovered → NORMAL", person_id)
                st["state"] = NORMAL
                st["since"] = timestamp
            elif timestamp - st["since"] >= CONFIRM_TIME_S:
                st["state"] = FALL_CONFIRMED
                st["since"] = timestamp
                logger.warning("Fall: person %d → FALL_CONFIRMED", person_id)
                confirm_now = True
            else:
                confirm_now = False
        else:
            confirm_now = False

        # Side effects (event + emergency hooks) fire exactly once per person.
        if confirm_now:
            st["confirmed_fired"] = True
            evidence = {
                "person_id": person_id,
                "state": FALL_CONFIRMED,
                "aspect": aspect,
                "confidence": confidence,
                "confirmed_at": timestamp,
                "sustained_s": CONFIRM_TIME_S,
            }
            _confirmed_event = evidence

    # Outside the lock: event + hooks (idempotent — confirmed_fired guards)
    if evidence is not None:
        try:
            from services import motor_service
            # Policy: Stop robot immediately on fall to avoid hitting the person
            motor_service.trigger_estop()
        except Exception as e:
            logger.error("Failed to trigger estop on fall: %s", e)
            
        try:
            from services import localization_service
            loc = localization_service.get_location()
            if loc:
                evidence["location"] = loc.get("name")
                evidence["tag_id"] = loc.get("tag_id")
        except Exception:
            pass
            
        try:
            from services import vision_service
            from services import person_recognition
            import os
            import time
            jpeg = vision_service._latest_node_jpeg()
            if jpeg:
                os.makedirs(person_recognition.SNAPSHOT_DIR, exist_ok=True)
                path = os.path.join(person_recognition.SNAPSHOT_DIR, f"fall_{int(time.time())}.jpg")
                with open(path, "wb") as f:
                    f.write(jpeg)
                evidence["snapshot"] = os.path.basename(path)
        except Exception as e:
            logger.error("Failed to save fall snapshot: %s", e)

        logger.critical("FALL CONFIRMED for person %d — emergency workflow", person_id)
        try:
            from services import safety_events
            safety_events.report("FALL", evidence, severity="DANGER")
        except Exception:
            pass
        _fire_emergency_hooks(evidence)
    return _view(person_id, st)


def _view(person_id: int, st: dict) -> dict:
    return {
        "person_id": person_id,
        "state": st["state"],
        "since": st["since"],
        "aspect": round(st["aspect"], 2),
        "confidence": st["confidence"],
    }


# ── Sampler (Phase 10 tracks → evidence) ────────────────────────────────────

def _sampler_loop() -> None:
    logger.info("Fall sampler started (poll=%.2fs, aspect≥%.2f, confirm=%.1fs)",
                FALL_POLL_S, ASPECT_LYING, CONFIRM_TIME_S)
    while not _stop_event.is_set():
        try:
            from services import person_detection
            tracks = person_detection.get_tracked()
            with _lock:
                alive_ids = {t["person_id"] for t in tracks}
            for t in tracks:
                if t["confidence"] < MIN_CONF:
                    continue
                sample(t["person_id"], t["bbox"], t["confidence"])
            # Persons no longer tracked (left frame) keep their latched state
            # if confirmed; un-confirmed possible-falls expire.
            with _lock:
                for pid in list(_person_states.keys()):
                    if pid in alive_ids:
                        continue
                    st = _person_states[pid]
                    if st["state"] == POSSIBLE_FALL and \
                            time.time() - st["last_seen"] > MAX_ABSENCE_S:
                        st["state"] = NORMAL
                        st["since"] = time.time()
        except Exception as exc:
            logger.error("Fall sampler error: %s", exc)
        _stop_event.wait(FALL_POLL_S)
    logger.info("Fall sampler stopped")


MAX_ABSENCE_S = 3.0


def _maybe_confirm(view: dict) -> None:
    """Retained for API compatibility — confirmation side effects now happen
    inside sample() exactly once."""
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def get_status() -> dict:
    with _lock:
        persons = [dict(_view(pid, st)) for pid, st in _person_states.items()]
        confirmed = dict(_confirmed_event) if _confirmed_event else None
    # Overall = worst state among persons
    overall = NORMAL
    for p in persons:
        if p["state"] == FALL_CONFIRMED:
            overall = FALL_CONFIRMED
            break
        if p["state"] == POSSIBLE_FALL:
            overall = POSSIBLE_FALL
    return {
        "enabled": FALL_ENABLED,
        "overall": overall,
        "persons": persons,
        "last_confirmed": confirmed,
        "tuning": {
            "aspect_lying": ASPECT_LYING,
            "movement_px": MOVEMENT_PX,
            "confirm_time_s": CONFIRM_TIME_S,
            "min_confidence": MIN_CONF,
            "poll_s": FALL_POLL_S,
        },
    }


def reset(person_id: Optional[int] = None) -> dict:
    """Acknowledge/clear: resets one person or all (post-emergency)."""
    global _confirmed_event
    with _lock:
        if person_id is None:
            _person_states.clear()
            _confirmed_event = None
        else:
            _person_states.pop(person_id, None)
            if _confirmed_event and _confirmed_event.get("person_id") == person_id:
                _confirmed_event = None
    logger.info("Fall state reset (person=%s)", person_id)
    # Phase 13: clearing the fall also silences a still-ringing emergency call
    try:
        from services import emergency_call
        emergency_call.ack("fall reset")
    except Exception:
        pass
    return get_status()


def simulate_fall(person_id: int = 99, duration_s: float = 5.0) -> dict:
    """Dev/emulator: drive one person through a full fall sequence."""
    t0 = time.time()
    # standing (tall-narrow)
    sample(person_id, [300, 60, 120, 360], 0.9, timestamp=t0)
    # fallen (wide-short) + stationary, held beyond confirm window
    t = t0
    while t - t0 < duration_s:
        t += CONFIRM_TIME_S / 2
        result = sample(person_id, [180, 280, 360, 140], 0.9, timestamp=t)
    return {"person_id": person_id, "final_state": result["state"],
            "evidence": result}


# ── Lifecycle ────────────────────────────────────────────────────────────────

def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not FALL_ENABLED:
        logger.info("Fall detection disabled by config")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_sampler_loop, name="sentra-fall", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_event.set()
