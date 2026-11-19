"""Thread-safe bounded domain event bus for SENTRA demo coordination."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Callable

_lock = threading.RLock()
_listeners: dict[str, list[Callable[[dict], None]]] = {}
_history: deque = deque(maxlen=500)
_recent: OrderedDict[str, float] = OrderedDict()
_MAX_RECENT_KEYS = 1000


def publish(event_type: str, data: dict | None = None, *, dedupe_key: str | None = None,
            dedupe_s: float = 0.0, severity: str = "INFO") -> dict:
    now = time.time()
    key = dedupe_key or f"{event_type}:{uuid.uuid4().hex}"
    with _lock:
        if dedupe_s > 0:
            expired = [
                recent_key
                for recent_key, expires_at in _recent.items()
                if now >= expires_at
            ]
            for recent_key in expired:
                _recent.pop(recent_key, None)
        expires_at = _recent.get(key)
        if dedupe_s > 0 and expires_at is not None and now < expires_at:
            return {"id": "", "type": event_type, "deduplicated": True}
        if dedupe_s > 0:
            _recent[key] = now + dedupe_s
            _recent.move_to_end(key)
            while len(_recent) > _MAX_RECENT_KEYS:
                _recent.popitem(last=False)
        event = {
            "id": f"EVT-{uuid.uuid4().hex[:12]}",
            "type": str(event_type),
            "severity": severity,
            "deduplicated": False,
            "timestamp": now,
            "data": dict(data or {}),
            "dedupe_key": key,
        }
        _history.append(event)
        listeners = list(_listeners.get(event_type, []))
        wildcard = list(_listeners.get("*", []))
    for listener in listeners + wildcard:
        try:
            listener(event)
        except Exception:
            continue
    return event


def subscribe(event_type: str, listener: Callable[[dict], None]) -> None:
    with _lock:
        _listeners.setdefault(event_type, [])
        if listener not in _listeners[event_type]:
            _listeners[event_type].append(listener)


def unsubscribe(event_type: str, listener: Callable[[dict], None]) -> None:
    with _lock:
        listeners = _listeners.get(event_type)
        if listeners and listener in listeners:
            listeners.remove(listener)


def history(limit: int = 100) -> list[dict]:
    with _lock:
        return [dict(event) for event in list(_history)[-max(0, limit):]]


def stats() -> dict:
    with _lock:
        return {
            "history_size": len(_history),
            "history_limit": _history.maxlen,
            "listeners": sum(len(values) for values in _listeners.values()),
            "dedupe_keys": len(_recent),
        }
