"""
SENTRA — Notification service (Phase 14).

Turns the backend's real safety/activity stream into user-facing
notifications. Every safety event (FALL, PERSON_UNKNOWN, PERSON, OBSTACLE,
CLIFF, ESTOP, PATROL, DOCK, NAVIGATION, …) is mirrored into a persistent,
acknowledgeable notification, and pushed live to connected apps over the
existing /ws/alerts channel as {"type":"notification", ...}.

The Flutter Alerts screen contract (APIS.md §10) is preserved exactly:
    GET  /api/v1/alerts?severity=ALL|DANGER|WARNING   → {alerts: [AlertItem]}
    POST /api/v1/alerts/{id}/ack                      → {alert_id, acknowledged}
with real notifications replacing the Phase 1 mock store.

Severity → presentation mapping (display strings kept stable for Flutter):
    DANGER  → "DANGER",  user title "Emergency: …"
    WARNING → "WARNING"
    INFO    → "INFO"     (presence / activity notes)

Persistence: ~/sentra_data/notifications.json (atomic tmp+rename writes,
bounded to HISTORY_MAX, loaded on startup) so the feed survives restarts.

Env vars:
    SENTRA_NOTIFICATIONS_ENABLED (default true)
    SENTRA_NOTIFICATIONS_PATH    (default ~/sentra_data/notifications.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

ENABLED = os.getenv("SENTRA_NOTIFICATIONS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
DEFAULT_PATH = os.path.join(os.path.expanduser("~"), "sentra_data",
                            "notifications.json")
PATH = os.path.expanduser(
    os.getenv("SENTRA_NOTIFICATIONS_PATH", DEFAULT_PATH))
HISTORY_MAX = 500

_lock = threading.Lock()
_notifications: deque = deque(maxlen=HISTORY_MAX)   # oldest first
_seq = 0
_loop = None

# safety event types worth surfacing as user notifications.
# (Every type in safety_events.EVENT_TYPES qualifies; PERSON is INFO-level.)
_TITLE_BY_EVENT = {
    "FALL": "Emergency: Fall detected",
    "PERSON_UNKNOWN": "Unknown person detected",
    "PERSON": "Person detected",
    "ESTOP": "Emergency stop engaged",
    "OBSTACLE": "Obstacle detected",
    "CLIFF": "Cliff/drop hazard detected",
    "TIMEOUT": "Watchdog timeout",
    "MODE_MISMATCH": "Mode mismatch resolved",
    "PATROL": "Patrol update",
    "DOCK": "Docking update",
    "NAVIGATION": "Navigation update",
}


def _display_timestamp(epoch: float, location: str) -> str:
    """Flutter contract: '<HH:MM:SS> • <Location>'."""
    return f"{time.strftime('%H:%M:%S', time.localtime(epoch))} • {location}"


def _new_id(epoch: float) -> str:
    global _seq
    _seq += 1
    return f"NOTIF-{int(epoch * 1000)}-{_seq}"


def _persist_locked() -> None:
    try:
        tmp = PATH + ".tmp"
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "notifications": list(_notifications)},
                      f, ensure_ascii=False)
        os.replace(tmp, PATH)
    except Exception as exc:
        logger.error("Notification persist failed: %s", exc)


def load(path: str | None = None) -> None:
    """Load persisted notifications (call once from lifespan)."""
    global PATH
    if path:
        PATH = os.path.expanduser(path)
    with _lock:
        _notifications.clear()
        try:
            with open(PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for n in data.get("notifications", [])[-HISTORY_MAX:]:
                _notifications.append(n)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Notification load failed (starting fresh): %s", exc)


def attach_loop(loop) -> None:
    """Call once from lifespan; enables thread-safe live pushes."""
    global _loop
    _loop = loop


def _push(notification: dict) -> None:
    """Fire-and-forget push onto /ws/alerts (any thread)."""
    loop = _loop
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(notification), loop)
    except RuntimeError:
        pass  # loop shutting down


async def _broadcast(notification: dict) -> None:
    from ws_handlers.alerts_ws import broadcast_alert
    try:
        await broadcast_alert({
            "type": "notification",
            "notification": notification,
        })
    except Exception as exc:
        logger.error("Notification broadcast failed: %s", exc)


def _detail_text(event_type: str, detail: dict, severity: str) -> str:
    pid = detail.get("person_id")
    if event_type == "FALL" and pid is not None:
        return (f"A fall was confirmed for person {pid} "
                f"(aspect {detail.get('aspect', '?')}, "
                f"confidence {detail.get('confidence', '?')}). "
                f"Emergency call flow engaged.")
    if event_type == "PERSON_UNKNOWN":
        return (f"{detail.get('faces', 1)} unrecognized face(s) seen. "
                f"Snapshot: {detail.get('snapshot') or 'n/a'}.")
    if event_type == "PERSON":
        ids = detail.get("person_ids") or []
        return f"{detail.get('persons', len(ids))} person(s) in view."
    text = ", ".join(f"{k}={v}" for k, v in sorted(detail.items())
                     if isinstance(v, (int, float, str, bool)))
    return text or f"{event_type} event reported ({severity.lower()})."


def from_safety_event(event: dict) -> dict | None:
    """Build a notification dict from a safety_events event."""
    etype = event.get("type", "")
    severity = event.get("severity", "WARNING")
    location = event.get("location", "unknown") or "unknown"
    created = event.get("timestamp") or time.time()
    title = _TITLE_BY_EVENT.get(etype, f"Safety: {etype.title()}")
    if severity == "DANGER" and not title.lower().startswith("emergency"):
        title = f"Emergency: {title}"
    detail = event.get("detail") or {}
    # Phase 3: keep the snapshot filename (person_recognition saves JPEGs of
    # unknown-person events) so the alert detail screen can show the image.
    image_file = detail.get("snapshot") or None
    return {
        "id": _new_id(created),
        "title": title,
        "timestamp": _display_timestamp(created, location),
        "description": _detail_text(etype, detail, severity),
        "severity": severity,
        "acknowledged": False,
        "event_type": etype,
        "location": location,
        "created_at": created,
        "acknowledged_at": None,
        "image_file": image_file,
    }


def add_manual(title: str, description: str, severity: str = "INFO",
               location: str = "robot") -> dict:
    """User/backend-created notification (e.g. test endpoint)."""
    now = time.time()
    n = {
        "id": _new_id(now),
        "title": title,
        "timestamp": _display_timestamp(now, location),
        "description": description,
        "severity": severity.upper() if severity.upper() in ("INFO", "WARNING", "DANGER") else "INFO",
        "acknowledged": False,
        "event_type": "MANUAL",
        "location": location,
        "created_at": now,
        "acknowledged_at": None,
    }
    with _lock:
        _notifications.append(n)
        _persist_locked()
    _push(n)
    logger.info("Notification (manual): %s", title)
    return dict(n)


def notify_safety_event(event: dict) -> dict | None:
    """Mirror one safety event into a notification (thread-safe).
    Passed to safety_events.add_listener(); exceptions never propagate."""
    if not ENABLED:
        return None
    try:
        n = from_safety_event(event)
        if n is None:
            return None
        with _lock:
            _notifications.append(n)
            _persist_locked()
        _push(n)
        return n
    except Exception as exc:
        logger.error("notify_safety_event failed: %s", exc)
        return None


def get(severity: str = "ALL", limit: int = 100) -> list[dict]:
    """Newest-first view (Flutter shows newest at top)."""
    with _lock:
        items = list(_notifications)
    if severity and severity.upper() != "ALL":
        items = [n for n in items if n["severity"] == severity.upper()]
    items.reverse()  # newest first
    return [dict(n) for n in items[:max(1, limit)]]


def ack(alert_id: str) -> bool:
    """Acknowledge one notification by id (accepts legacy ALT-* ids too)."""
    with _lock:
        for n in reversed(_notifications):
            if n["id"] == alert_id:
                if not n["acknowledged"]:
                    n["acknowledged"] = True
                    n["acknowledged_at"] = time.time()
                    _persist_locked()
                return True
    return False


def ack_all() -> int:
    with _lock:
        count = 0
        now = time.time()
        for n in _notifications:
            if not n["acknowledged"]:
                n["acknowledged"] = True
                n["acknowledged_at"] = now
                count += 1
        if count:
            _persist_locked()
    return count


def unread_count() -> int:
    with _lock:
        return sum(1 for n in _notifications if not n["acknowledged"])


def stats() -> dict:
    with _lock:
        total = len(_notifications)
        unack = sum(1 for n in _notifications if not n["acknowledged"])
        by_sev = {"DANGER": 0, "WARNING": 0, "INFO": 0}
        for n in _notifications:
            by_sev[n["severity"]] = by_sev.get(n["severity"], 0) + 1
    return {
        "enabled": ENABLED,
        "total": total,
        "unread": unack,
        "by_severity": by_sev,
        "path": PATH,
    }


def clear() -> None:
    """Dev/test helper: drop all notifications."""
    with _lock:
        _notifications.clear()
        _persist_locked()
