"""
SENTRA — Safety event service (Phase 3).

The safety layer (thread world) reports events here; this service debounces
repeats (obstacle events fire every loop tick otherwise), keeps a bounded
history, and bridges events to the async alerts WebSocket (`/ws/alerts`)
via a thread-safe queue pumped by one asyncio task.

Event types:
    ESTOP, OBSTACLE, CLIFF, TIMEOUT, MODE_MISMATCH, PATROL, DOCK, NAVIGATION,
    PERSON, PERSON_UNKNOWN, FALL
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

EVENT_TYPES = {"ESTOP", "OBSTACLE", "CLIFF", "TIMEOUT", "MODE_MISMATCH", "PATROL", "DOCK", "NAVIGATION", "PERSON",
               "PERSON_UNKNOWN", "FALL"}
HISTORY_MAX = 200
# Same-cause events within this window are collapsed into the first.
DEBOUNCE_S = 2.0

_history: deque = deque(maxlen=HISTORY_MAX)
_counters: dict[str, int] = {t: 0 for t in EVENT_TYPES}
_lock = threading.Lock()
_last_event_at: dict[str, float] = {}
_listeners: list = []  # Phase 14: extra consumers (e.g. notification service)


def add_listener(fn) -> None:
    """Register fn(event_dict); called for every reported event (any thread)."""
    _listeners.append(fn)

# Async bridge
_queue: "asyncio.Queue[dict] | None" = None
_loop = None
_pump_task = None


def attach_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Call once from lifespan with the running event loop."""
    global _queue, _loop, _pump_task
    if _queue is not None:
        return
    _loop = loop
    _queue = asyncio.Queue()
    _pump_task = loop.create_task(_pump())


async def _pump() -> None:
    """Forward queued events to every alerts-WS subscriber."""
    from ws_handlers.alerts_ws import broadcast_alert
    while True:
        event = await _queue.get()
        try:
            await broadcast_alert({
                "type": "safety_event",
                "event": event["type"],
                "severity": event["severity"],
                "detail": event["detail"],
                "timestamp": event["timestamp"],
            })
        except Exception as exc:
            logger.error("safety event pump error: %s", exc)


def report(event_type: str, detail: dict | None = None,
           severity: str = "WARNING", location: str = "unknown") -> dict | None:
    """
    Record a safety event (thread-safe, callable from any thread).
    Debounces identical types within DEBOUNCE_S. Returns the event if new.
    """
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown safety event type: %s", event_type)
        return None

    now = time.time()
    with _lock:
        last = _last_event_at.get(event_type, 0.0)
        if now - last < DEBOUNCE_S:
            return None
        _last_event_at[event_type] = now
        _counters[event_type] += 1
        event = {
            "id": f"SAFE-{int(now * 1000)}",
            "type": event_type,
            "severity": severity,
            "location": location,
            "detail": detail or {},
            "timestamp": now,
        }
        _history.append(event)

    _enqueue(event)
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception as exc:
            logger.error("Safety event listener failed: %s", exc)
    logger.info("Safety event: %s %s", event_type, detail or "")
    return event


def _enqueue(event: dict) -> None:
    q, loop = _queue, _loop
    if q is None or loop is None:
        return
    try:
        loop.call_soon_threadsafe(q.put_nowait, event)
    except RuntimeError:
        pass  # loop shutting down


def get_history(limit: int = 50) -> list:
    with _lock:
        return list(_history)[-limit:]


def get_counters() -> dict:
    with _lock:
        return dict(_counters)


def stats() -> dict:
    with _lock:
        last = dict(_last_event_at)
    return {
        "counters": get_counters(),
        "history_size": len(_history),
        "last_event_types_age_s": {
            t: round(time.time() - ts, 1) for t, ts in last.items()
        },
    }
