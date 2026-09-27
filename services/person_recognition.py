"""
SENTRA — Person recognition service (Phase 11).

Face pipeline on phone frames (runs in its own worker, newest-frame only):

    vision hub → face detector (Haar) → face crops → embedder backend
              → cosine match vs registry → KNOWN (name) | UNKNOWN → alert

Embedder backends (pluggable — swap for FaceNet/Arcane later, API unchanged):
    "simple" (default) — dependency-free: 32×32 grayscale normalized vector,
                         cosine similarity. Decent for a fixed home camera angle.
    "simulation"       — inject-only for tests/emulators.

Unknown persons: UNKNOWN_ALERT_COOLDOWN_S debounce → snapshot JPEG saved +
PERSON_UNKNOWN event pushed to /ws/alerts (Phase 14 notification service will
consume these too).

Env vars:
    SENTRA_RECOG_ENABLED        (default true)
    SENTRA_RECOG_BACKEND        (simple | simulation)
    SENTRA_RECOG_MATCH_THRESH   (default 0.86 cosine similarity)
    SENTRA_RECOG_POLL_S         (default 0.2)
    SENTRA_RECOG_UNKNOWN_COOLDOWN_S (default 15)
    SENTRA_RECOG_SNAPSHOT_DIR   (default ~/sentra_snapshots/persons)
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    _CV_OK = True
except ImportError:
    _CV_OK = False

from services import safety_events

RECOG_ENABLED = os.getenv("SENTRA_RECOG_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
RECOG_BACKEND = os.getenv("SENTRA_RECOG_BACKEND", "simple").strip().lower()
MATCH_THRESH = float(os.getenv("SENTRA_RECOG_MATCH_THRESH", "0.86"))
POLL_S = float(os.getenv("SENTRA_RECOG_POLL_S", "0.2"))
UNKNOWN_COOLDOWN_S = float(os.getenv("SENTRA_RECOG_UNKNOWN_COOLDOWN_S", "15"))
SNAPSHOT_DIR = os.path.expanduser(
    os.getenv("SENTRA_RECOG_SNAPSHOT_DIR", "~/sentra_snapshots/persons"))
FACE_SIZE = 32  # simple embedder input

_haar = None
_haar_failed = False
_last_unknown_at = 0.0
_stats = {
    "frames_seen": 0,
    "faces_seen": 0,
    "matches": 0,
    "unknown_alerts": 0,
    "last_face_age_s": None,
}
_stats_lock = threading.Lock()
_queue: deque = deque(maxlen=1)
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _get_haar():
    global _haar, _haar_failed
    if _haar is None and not _haar_failed and _CV_OK:
        try:
            path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            _haar = cv2.CascadeClassifier(path)
            if _haar.empty():
                _haar = None
                _haar_failed = True
        except Exception as exc:
            logger.warning("Haar cascade unavailable: %s", exc)
            _haar_failed = True
    return _haar


# ── Embedder backends ────────────────────────────────────────────────────────

def embed_face(face_img) -> Optional[list[float]]:
    """Face crop (BGR) → embedding vector. None if the backend can't run."""
    if RECOG_BACKEND == "simulation":
        return None
    if not _CV_OK or face_img is None:
        return None
    try:
        gray = cv2.cvtColor(face_img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (FACE_SIZE, FACE_SIZE))
        vec = gray.astype("float32") / 255.0
        vec = (vec - vec.mean()) / (vec.std() + 1e-6)  # contrast-normalize
        return [round(float(v), 5) for v in vec.flatten()]
    except Exception as exc:
        logger.error("embed_face error: %s", exc)
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def match_embedding(embedding: list[float]) -> Optional[dict]:
    """Best cosine match above threshold. Returns {person_key, name, similarity}."""
    from services import person_registry
    best = None
    for key, name, ref in person_registry.all_embeddings():
        sim = cosine_similarity(embedding, ref)
        if sim >= MATCH_THRESH and (best is None or sim > best["similarity"]):
            best = {"person_key": key, "name": name, "similarity": round(sim, 3)}
    return best


# ── Face detection ───────────────────────────────────────────────────────────

def detect_faces(frame) -> list[tuple[int, int, int, int]]:
    """Haar frontal-face detection → [(x, y, w, h)]."""
    cascade = _get_haar()
    if cascade is None or frame is None:
        return []
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                         minSize=(48, 48))
        return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in faces]
    except Exception as exc:
        logger.error("detect_faces error: %s", exc)
        return []


def recognize_frame(frame) -> dict:
    """
    One-shot recognition on a full frame (used by POST /persons/recognize
    and by the worker). Returns:
        {faces: [{bbox, match: {...}|None}], unknown_count}
    """
    global _last_unknown_at
    if frame is None:
        return {"faces": [], "unknown_count": 0}
    faces_out = []
    unknown = 0
    now = time.time()
    for (x, y, w, h) in detect_faces(frame):
        crop = frame[max(0, y):y + h, max(0, x):x + w]
        embedding = embed_face(crop)
        match = match_embedding(embedding) if embedding else None
        if match is None and embedding is not None:
            unknown += 1
        faces_out.append({
            "bbox": [x, y, w, h],
            "match": match,
            "known": match is not None,
        })
        with _stats_lock:
            _stats["faces_seen"] += 1

    if unknown > 0 and (now - _last_unknown_at) >= UNKNOWN_COOLDOWN_S:
        _last_unknown_at = now
        path = _save_unknown_snapshot(frame)
        with _stats_lock:
            _stats["unknown_alerts"] += 1
        try:
            safety_events.report("PERSON_UNKNOWN", {
                "faces": unknown,
                "snapshot": os.path.basename(path) if path else None,
            }, severity="DANGER")
        except Exception:
            pass
    return {"faces": faces_out, "unknown_count": unknown}


def _save_unknown_snapshot(frame) -> Optional[str]:
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOT_DIR, f"unknown_{int(time.time())}.jpg")
        import cv2
        cv2.imwrite(path, frame)
        return path
    except Exception as exc:
        logger.warning("Unknown snapshot save failed: %s", exc)
        return None


# ── Worker (vision hub subscriber) ───────────────────────────────────────────

def _on_frame(frame, frame_id: str) -> None:
    try:
        if len(_queue) == _queue.maxlen:
            _queue.popleft()
        _queue.append((frame, frame_id))
    except Exception:
        pass


def _worker_loop() -> None:
    logger.info("Person recognition worker started (backend=%s, poll=%.2fs)",
                RECOG_BACKEND, POLL_S)
    while not _stop_event.is_set():
        try:
            frame, _fid = _queue.popleft()
        except IndexError:
            _stop_event.wait(POLL_S)
            continue
        with _stats_lock:
            _stats["frames_seen"] += 1
        try:
            recognize_frame(frame)
            with _stats_lock:
                if _stats["faces_seen"] > 0:
                    _stats["last_face_age_s"] = 0.0
        except Exception as exc:
            logger.error("Recognition worker error: %s", exc)
        _stop_event.wait(POLL_S)
    logger.info("Person recognition worker stopped")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not RECOG_ENABLED:
        logger.info("Person recognition disabled by config")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker_loop, name="sentra-recog", daemon=True)
    _thread.start()
    from services import vision_service
    vision_service.subscribe(_on_frame, name="person_recognition")
    logger.info("Person recognition subscribed to vision hub")


def stop() -> None:
    _stop_event.set()


def stats() -> dict:
    from services import person_registry
    with _stats_lock:
        s = dict(_stats)
    s.update({
        "enabled": RECOG_ENABLED,
        "backend": RECOG_BACKEND,
        "opencv_available": _CV_OK,
        "haar_available": _get_haar() is not None,
        "match_threshold": MATCH_THRESH,
        "registered_persons": person_registry.count(),
        "unknown_cooldown_s": UNKNOWN_COOLDOWN_S,
    })
    return s
