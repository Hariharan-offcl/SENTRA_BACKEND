"""
SENTRA — Person registry (Phase 11).

Persistent store of registered persons and their reference embeddings
(NOT raw images — embeddings are small vectors; one reference snapshot is
kept per registration for the app gallery).

Storage: data/person_registry.json (atomic writes, thread-safe).
Shape:
    {"persons": [{"person_key": "...", "name": "...", "embeddings": [...],
                  "snapshot_path": "...", "created_at": ..., "updated_at": ...}]}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_PATH = os.path.expanduser(
    os.getenv("SENTRA_PERSON_REGISTRY_PATH", os.path.join(DATA_DIR, "person_registry.json")))

_lock = threading.RLock()
_path = DEFAULT_PATH
_persons: dict[str, dict] = {}   # person_key → record
_key_by_name: dict[str, str] = {}


def _write_locked() -> None:
    tmp = _path + ".tmp"
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "persons": list(_persons.values())},
                  f, ensure_ascii=False)
    os.replace(tmp, _path)


def load(path: str = DEFAULT_PATH) -> None:
    global _path, _persons, _key_by_name
    with _lock:
        _path = path
        _persons = {}
        _key_by_name = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for p in data.get("persons", []):
                    p["embeddings"] = [list(e) for e in p.get("embeddings", [])]
                    _persons[p["person_key"]] = p
                    _key_by_name[p["name"].lower()] = p["person_key"]
                logger.info("Person registry loaded: %d persons", len(_persons))
                return
            except Exception as exc:
                logger.error("Person registry load failed (%s) — starting empty", exc)


def _reindex_locked() -> None:
    _key_by_name.clear()
    for key, p in _persons.items():
        _key_by_name[p["name"].lower()] = key


def list_persons() -> list[dict]:
    with _lock:
        out = []
        for p in _persons.values():
            d = dict(p)
            d["embedding_count"] = len(p["embeddings"])
            d.pop("embeddings", None)  # vectors stay internal
            out.append(d)
    out.sort(key=lambda p: p["created_at"])
    return out


def get_person(person_key: str) -> Optional[dict]:
    with _lock:
        p = _persons.get(person_key)
        if p is None:
            return None
        d = dict(p)
        d["embedding_count"] = len(p["embeddings"])
        d.pop("embeddings", None)
        return d


def find_by_name(name: str) -> Optional[dict]:
    with _lock:
        key = _key_by_name.get(name.strip().lower())
        return get_person(key) if key else None


def add_person(name: str, embeddings: list[list[float]],
               snapshot_path: Optional[str] = None, notes: str = "") -> dict:
    """Register a person. Rejects duplicate names (use update for that)."""
    with _lock:
        if not name or not name.strip():
            raise ValueError("name must not be empty")
        name_key = name.strip().lower()
        if name_key in _key_by_name:
            raise ValueError(f"person already registered: {name}")
        if not embeddings:
            raise ValueError("at least one embedding is required")
        key = uuid.uuid4().hex[:12]
        record = {
            "person_key": key,
            "name": name.strip(),
            "notes": (notes or "").strip(),
            "embeddings": [list(e) for e in embeddings],
            "snapshot_path": snapshot_path,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _persons[key] = record
        _reindex_locked()
        _write_locked()
        logger.info("Person registered: %s (%s) with %d embeddings",
                    name, key, len(record["embeddings"]))
        d = dict(record)
        d["embedding_count"] = len(record["embeddings"])
        d.pop("embeddings", None)
        return d


def update_person(person_key: str, name: Optional[str] = None,
                  notes: Optional[str] = None,
                  snapshot_path: Optional[str] = None) -> dict:
    """Update name/notes/snapshot (Phase 21: app PUT /people/{id}).
    Fields left as None keep their current value; a new snapshot_path replaces
    the stored one (old file removed)."""
    with _lock:
        p = _persons.get(person_key)
        if p is None:
            raise KeyError("unknown person_key")
        if name is not None:
            if not name.strip():
                raise ValueError("name must not be empty")
            new_key = name.strip().lower()
            if new_key in _key_by_name and _key_by_name[new_key] != person_key:
                raise ValueError(f"person already registered: {name}")
            p["name"] = name.strip()
        if notes is not None:
            p["notes"] = notes.strip()
        old_snap = p.get("snapshot_path")
        if snapshot_path and snapshot_path != old_snap:
            p["snapshot_path"] = snapshot_path
            if old_snap and os.path.exists(old_snap):
                try:
                    os.remove(old_snap)
                except OSError:
                    pass
        p["updated_at"] = time.time()
        _reindex_locked()
        _write_locked()
        logger.info("Person updated: %s (%s)", p["name"], person_key)
        d = dict(p)
        d["embedding_count"] = len(p["embeddings"])
        d.pop("embeddings", None)
        return d


def add_embedding(person_key: str, embedding: list[float]) -> dict:
    """Add another reference embedding (improves matching over time)."""
    with _lock:
        p = _persons.get(person_key)
        if p is None:
            raise KeyError("unknown person_key")
        p["embeddings"].append(list(embedding))
        p["updated_at"] = time.time()
        _write_locked()
        d = dict(p)
        d["embedding_count"] = len(p["embeddings"])
        d.pop("embeddings", None)
        return d


def delete_person(person_key: str) -> bool:
    with _lock:
        p = _persons.pop(person_key, None)
        if p is None:
            return False
        _reindex_locked()
        _write_locked()
    snap = p.get("snapshot_path")
    if snap and os.path.exists(snap):
        try:
            os.remove(snap)
        except OSError:
            pass
    logger.info("Person deleted: %s (%s)", p["name"], person_key)
    return True


def all_embeddings() -> list[tuple[str, str, list[float]]]:
    """Flat list of (person_key, name, embedding) for the matcher."""
    with _lock:
        return [(key, p["name"], list(e))
                for key, p in _persons.items() for e in p["embeddings"]]


def count() -> int:
    with _lock:
        return len(_persons)
