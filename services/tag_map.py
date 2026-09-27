"""
SENTRA — Tag map (Phase 5).

Persistent, user-configurable mapping of AprilTag IDs → locations.
NO hardcoded location names — the defaults are created on first run only,
renamed/deleted freely via the API, and stored in data/tag_map.json.

Structure per entry:
    {"tag_id": 2, "name": "Kitchen", "type": "LOCATION", "notes": "",
     "created_at": ..., "updated_at": ...}

Types: LOCATION (a named place) | DOCK (return-to-dock target, Phase 8).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_PATH = os.path.join(DATA_DIR, "tag_map.json")

# First-run defaults only — fully editable afterwards.
DEFAULT_TAGS = [
    {"tag_id": 1, "name": "Dock", "type": "DOCK"},
    {"tag_id": 2, "name": "Kitchen", "type": "LOCATION"},
    {"tag_id": 3, "name": "Bedroom", "type": "LOCATION"},
    {"tag_id": 4, "name": "Hall", "type": "LOCATION"},
    {"tag_id": 5, "name": "Living Room", "type": "LOCATION"},
]

VALID_TYPES = {"LOCATION", "DOCK"}
_lock = threading.RLock()
_path: str = DEFAULT_PATH
_tags: dict[int, dict] = {}


def _now() -> float:
    return time.time()


def _write_locked() -> None:
    tmp = _path + ".tmp"
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "tags": sorted(_tags.values(), key=lambda t: t["tag_id"])},
                  f, indent=2, ensure_ascii=False)
    os.replace(tmp, _path)


def load(path: str = DEFAULT_PATH) -> None:
    """Load the tag map (or create defaults on first run)."""
    global _path, _tags
    with _lock:
        _path = path
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _tags = {int(t["tag_id"]): dict(t) for t in data.get("tags", [])}
                logger.info("Tag map loaded: %d tags from %s", len(_tags), path)
                return
            except Exception as exc:
                logger.error("Tag map load failed (%s) — recreating defaults", exc)
        _tags = {}
        for t in DEFAULT_TAGS:
            _tags[t["tag_id"]] = {**t, "notes": "", "created_at": _now(), "updated_at": _now()}
        _write_locked()
        logger.info("Tag map created with %d defaults at %s", len(_tags), path)


def list_tags() -> list[dict]:
    with _lock:
        return sorted((dict(t) for t in _tags.values()), key=lambda t: t["tag_id"])


def get_tag(tag_id: int) -> Optional[dict]:
    with _lock:
        t = _tags.get(int(tag_id))
        return dict(t) if t else None


def find_by_name(name: str) -> Optional[dict]:
    """Case-insensitive name lookup (used by voice commands in Phase 9)."""
    needle = name.strip().lower()
    with _lock:
        for t in _tags.values():
            if t["name"].strip().lower() == needle:
                return dict(t)
    return None


def upsert_tag(tag_id: int, name: str, type: str = "LOCATION",
               notes: str = "") -> dict:
    """Create or update a tag binding. Raises ValueError on bad input."""
    tag_id = int(tag_id)
    if not (0 <= tag_id <= 586):  # 36h11 family range
        raise ValueError("tag_id out of range (0..586 for 36h11)")
    if not name or not name.strip():
        raise ValueError("name must not be empty")
    type = type.upper()
    if type not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}")

    with _lock:
        existing = _tags.get(tag_id)
        entry = {
            "tag_id": tag_id,
            "name": name.strip(),
            "type": type,
            "notes": notes.strip(),
            "created_at": existing["created_at"] if existing else _now(),
            "updated_at": _now(),
        }
        _tags[tag_id] = entry
        _write_locked()
        return dict(entry)


def delete_tag(tag_id: int) -> bool:
    with _lock:
        if int(tag_id) in _tags:
            del _tags[int(tag_id)]
            _write_locked()
            return True
        return False


def describe_tag(tag_id: int) -> Optional[dict]:
    """'Which known location is this tag?' — None if unregistered."""
    return get_tag(tag_id)


def count() -> int:
    with _lock:
        return len(_tags)
