"""
SENTRA — Emergency call service (Phase 13).

Bridges Phase 12 fall confirmation to the Phase 1 call pipeline. When
fall_detection confirms a fall, this service opens an emergency session and
invites the caregiver app over the existing /ws/alerts channel. The app then
connects its normal call WebSockets (/ws/webrtc/{role}, /ws/call/{role}) —
there is NO new call protocol; roles stay "node" (rover phone A) and "user"
(caregiver phone B).

Session lifecycle:
    RINGING — invite broadcast; waiting for the caregiver to answer
    ACTIVE  — user (caregiver) signaling socket connected
    MISSED  — nobody answered within RING_TIMEOUT_S (lazy deadline check)
    ENDED   — ack while ringing, or all peers left an active call

One session at a time. A new confirmation while RINGING/ACTIVE only refreshes
the evidence (no duplicate invite). After a terminal state, COOLDOWN_S
suppresses duplicate sessions.

Env vars:
    SENTRA_EMERGENCY_ENABLED    (default true)
    SENTRA_EMERGENCY_RING_S     (default 30)
    SENTRA_EMERGENCY_COOLDOWN_S (default 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

from services import fall_detection

ENABLED = os.getenv("SENTRA_EMERGENCY_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
RING_TIMEOUT_S = float(os.getenv("SENTRA_EMERGENCY_RING_S", "30"))
COOLDOWN_S = float(os.getenv("SENTRA_EMERGENCY_COOLDOWN_S", "60"))
HISTORY_MAX = 50

RINGING = "RINGING"
ACTIVE = "ACTIVE"
MISSED = "MISSED"
ENDED = "ENDED"

_lock = threading.RLock()
_session: Optional[dict] = None
_history: deque = deque(maxlen=HISTORY_MAX)
_loop = None
_hook_registered = False


def attach_loop(loop) -> None:
    """Call once from lifespan; enables thread-safe invite broadcasts."""
    global _loop
    _loop = loop


def register_with_fall() -> None:
    """Idempotently wire on_fall_confirmed into fall_detection's hooks."""
    global _hook_registered
    if _hook_registered:
        return
    _hook_registered = True
    fall_detection.register_emergency_hook(on_fall_confirmed)


def on_fall_confirmed(evidence: dict) -> Optional[dict]:
    """Emergency hook (sync, called from the fall sampler thread).

    Opens a RINGING session (or refreshes the active one) and schedules an
    invite broadcast on the main loop. Never raises to the caller.
    """
    if not ENABLED:
        return None
    now = time.time()
    with _lock:
        global _session
        s = _session
        if s is not None and s["state"] in (RINGING, ACTIVE):
            s["evidence"] = dict(evidence)   # refresh; no duplicate invite
            logger.info("EMERGENCY CALL: session %s evidence refreshed", s["session_id"])
            return dict(s)
        if s is not None and s["state"] in (MISSED, ENDED) \
                and now - (s["ended_at"] or 0.0) < COOLDOWN_S:
            return dict(s)                   # cooldown window
        session = {
            "session_id": f"EMG-{int(now * 1000)}",
            "state": RINGING,
            "reason": "FALL",
            "acknowledged": False,
            "note": "",
            "started_at": now,
            "ended_at": None,
            "node_connected": False,
            "user_connected": False,
            "evidence": dict(evidence),
        }
        _session = session
        logger.warning("EMERGENCY CALL: session %s opened (fall confirmed, person %s)",
                       session["session_id"], evidence.get("person_id"))
    _schedule_broadcast("invite", session)
    return dict(session)


def _expire_locked(now: float) -> Optional[str]:
    """Lazy ring-deadline check (no timer thread). Returns 'missed' if fired."""
    s = _session
    if s is None or s["state"] != RINGING:
        return None
    if now - s["started_at"] <= RING_TIMEOUT_S:
        return None
    s["state"] = MISSED
    s["ended_at"] = now
    _history.append(dict(s))
    logger.warning("EMERGENCY CALL: session %s MISSED (no answer in %.0fs)",
                   s["session_id"], RING_TIMEOUT_S)
    return "missed"


def note_presence(role: str, present: bool) -> None:
    """Called by /ws/webrtc/{role} connect/disconnect (any thread/loop)."""
    now = time.time()
    broadcasts: list[tuple[str, dict]] = []
    with _lock:
        global _session
        fired = _expire_locked(now)
        if fired:
            broadcasts.append((fired, _session))
        s = _session
        if s is None or s["state"] in (MISSED, ENDED):
            s = None
        else:
            if role == "user":
                s["user_connected"] = present
            elif role == "node":
                s["node_connected"] = present
            else:
                return
            if s["state"] == RINGING and present and role == "user":
                s["state"] = ACTIVE
                logger.info("EMERGENCY CALL: session %s answered — ACTIVE",
                            s["session_id"])
            elif s["state"] == ACTIVE and not (s["node_connected"] or s["user_connected"]):
                s["state"] = ENDED
                s["ended_at"] = now
                _history.append(dict(s))
                logger.info("EMERGENCY CALL: session %s ENDED (peers gone)",
                            s["session_id"])
                broadcasts.append(("ended", s))
    for action, sess in broadcasts:
        _schedule_broadcast(action, sess)


def ack(note: str = "") -> Optional[dict]:
    """Caregiver acknowledged the emergency. Ends a still-ringing session;
    an active call keeps running until its peers disconnect."""
    now = time.time()
    broadcasts: list[tuple[str, dict]] = []
    result = None
    with _lock:
        global _session
        fired = _expire_locked(now)
        if fired:
            broadcasts.append((fired, _session))
        s = _session
        if s is not None and s["state"] in (RINGING, ACTIVE):
            s["acknowledged"] = True
            s["note"] = note
            if s["state"] == RINGING:
                s["state"] = ENDED
                s["ended_at"] = now
                _history.append(dict(s))
            logger.info("EMERGENCY CALL: session %s acknowledged", s["session_id"])
            result = dict(s)
            broadcasts.append(("acknowledged", s))
    for action, sess in broadcasts:
        _schedule_broadcast(action, sess)
    return result


def get_status() -> dict:
    now = time.time()
    broadcasts: list[tuple[str, dict]] = []
    with _lock:
        global _session
        fired = _expire_locked(now)
        if fired:
            broadcasts.append((fired, _session))
        s = _session
        session = dict(s) if s and s["state"] in (RINGING, ACTIVE) else None
        history = [dict(h) for h in _history]
    for action, sess in broadcasts:
        _schedule_broadcast(action, sess)
    return {
        "enabled": ENABLED,
        "session": session,
        "last_session": history[-1] if history else None,
        "tuning": {"ring_timeout_s": RING_TIMEOUT_S, "cooldown_s": COOLDOWN_S},
    }


def get_history() -> list:
    with _lock:
        return [dict(h) for h in _history]


# ── /ws/alerts broadcast (thread-safe bridge, same pattern as safety_events) ──

def _payload(action: str, session: dict) -> dict:
    payload = {
        "type": "emergency_call",
        "action": action,           # invite | acknowledged | missed | ended
        "session_id": session["session_id"],
        "reason": session["reason"],
        "severity": "DANGER",
        "state": session["state"],
        "evidence": session["evidence"],
        "timestamp": time.time(),
    }
    if action == "invite":
        payload["ws"] = {
            "alerts": "/ws/alerts",
            "signaling": "/ws/webrtc/user",
            "video": "/ws/call/user",
        }
    return payload


def _schedule_broadcast(action: str, session: Optional[dict]) -> None:
    loop = _loop
    if loop is None or session is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(action, session), loop)
    except RuntimeError:
        pass  # loop shutting down


async def _broadcast(action: str, session: dict) -> None:
    from ws_handlers.alerts_ws import broadcast_alert
    try:
        await broadcast_alert(_payload(action, session))
    except Exception as exc:
        logger.error("Emergency call broadcast failed: %s", exc)
