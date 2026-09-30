"""
SENTRA — Person detection service (Phase 10).

Modular detector pipeline on the phone-camera frames (vision hub):

    vision_service → person_detection worker thread → detector backend
                                                   → centroid tracker
                                                   → detections + events

Detector backends (pluggable — replace later without touching the pipeline):
    "simulation" — inject via POST /person/simulate or inject(); dev/emulator
    "hog"        — OpenCV HOG pedestrian detector (CPU, no extra deps)
    "yolo"       — YOLOv8n/YOLO11n ONNX export (recommended: far more accurate
                   at similar CPU cost; runs via onnxruntime when installed,
                   else through OpenCV's DNN module — no extra dependency)

Outputs per tracked person: person_id, bbox, confidence, timestamp, frame_id.
Detector failures degrade to "no persons" — never crash the pipeline.
Every N frames the worker reports a PERSON event to /ws/alerts.

Env vars:
    SENTRA_PERSON_ENABLED     (default true)
    SENTRA_PERSON_BACKEND     (default auto: auto|yolo|hog|simulation — auto
                               prefers yolo when the ONNX model + a runtime
                               exist, else hog, else simulation)
    SENTRA_PERSON_YOLO_MODEL  (default data/models/yolov8n.onnx — export with
                               `yolo export model=yolov8n.pt format=onnx
                               imgsz=320`, then copy to the Pi)
    SENTRA_PERSON_YOLO_IMGSZ  (default 320 — square input size; 320 is the
                               Pi 5 sweet spot, 640 for max accuracy)
    SENTRA_PERSON_YOLO_CONF   (default 0.40 — model confidence cutoff)
    SENTRA_PERSON_YOLO_NMS    (default 0.45 — NMS IoU threshold)
    SENTRA_PERSON_YOLO_FALLBACK (default true — degrade to HOG on any YOLO
                               load/inference failure)
    SENTRA_PERSON_POLL_S      (default 0.1 — worker pull rate)
    SENTRA_PERSON_MAX_AGE_S   (default 1.5 — tracked person considered gone)
    SENTRA_PERSON_MIN_CONF    (default 0.5)
    SENTRA_PERSON_EVENT_EVERY (default 3 — one event per N tracked updates)
    SENTRA_PERSON_HOG_WIDTH   (default 640 — frames are downscaled to this
                               width before detection; lower = faster)
    SENTRA_PERSON_HOG_WINSTRIDE (default 8 — HOG window stride; higher = faster,
                               coarser)
    SENTRA_PERSON_HOG_SCALE   (default 1.05 — HOG image pyramid scale)
"""

from __future__ import annotations

import logging
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

try:
    import onnxruntime as _ort
    _ORT_OK = True
except ImportError:
    _ORT_OK = False

from services import safety_events

PERSON_ENABLED = os.getenv("SENTRA_PERSON_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
PERSON_BACKEND = os.getenv("SENTRA_PERSON_BACKEND", "auto").strip().lower()
POLL_S = float(os.getenv("SENTRA_PERSON_POLL_S", "0.1"))
MAX_AGE_S = float(os.getenv("SENTRA_PERSON_MAX_AGE_S", "1.5"))
MIN_CONF = float(os.getenv("SENTRA_PERSON_MIN_CONF", "0.5"))
EVENT_EVERY = int(os.getenv("SENTRA_PERSON_EVENT_EVERY", "3"))

# Phase 6: HOG tuning knobs (Pi 5 CPU budget: detection runs on the worker
# thread at ~10 Hz; ~640px + stride 8 lands in the tens-of-ms range on x86
# and a few hundred ms on the Pi — measure via stats().avg_detect_ms).
HOG_DOWNSCALE_W = max(160, int(os.getenv("SENTRA_PERSON_HOG_WIDTH", "640")))
HOG_WINSTRIDE = max(2, int(os.getenv("SENTRA_PERSON_HOG_WINSTRIDE", "8")))
HOG_SCALE = max(1.01, float(os.getenv("SENTRA_PERSON_HOG_SCALE", "1.05")))

# YOLO backend tuning. Model path is resolved relative to the backend root
# when given as a relative path, so the service's cwd doesn't matter.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YOLO_MODEL_PATH = os.path.join(
    _BACKEND_ROOT,
    os.getenv("SENTRA_PERSON_YOLO_MODEL",
              os.path.join("data", "models", "yolov8n.onnx")))
YOLO_INPUT_PX = max(160, int(os.getenv("SENTRA_PERSON_YOLO_IMGSZ", "320")))
YOLO_CONF = float(os.getenv("SENTRA_PERSON_YOLO_CONF", "0.40"))
YOLO_NMS_IOU = float(os.getenv("SENTRA_PERSON_YOLO_NMS", "0.45"))
YOLO_FALLBACK_TO_HOG = os.getenv("SENTRA_PERSON_YOLO_FALLBACK", "true").strip().lower() in (
    "1", "true", "yes", "on")

HISTORY_MAX = 100
QUEUE_MAX = 1  # worker processes the newest frame only

_lock = threading.RLock()
_history: deque = deque(maxlen=HISTORY_MAX)
_tracks: dict[int, dict] = {}      # person_id → {bbox, confidence, last_seen, frames}
_next_pid = 1
_frames_seen = 0
_updates_since_event = 0
_backend_status = "unavailable"    # unavailable | simulation | hog | failed
_resolved_backend = "unavailable"  # what the worker actually picked (auto → hog/sim)
_detect_ms_ema: Optional[float] = None   # exponential average of detect time
_last_detect_ms: Optional[float] = None
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_queue: deque = deque(maxlen=QUEUE_MAX)

# HOG detector is built lazily on first frame (expensive)
_hog = None


# ── Detector backends ────────────────────────────────────────────────────────

def _get_hog():
    global _hog
    if _hog is None and _CV_OK:
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        _hog = hog
    return _hog


def _detect_hog(frame) -> list[dict]:
    """OpenCV HOG pedestrian detection. Returns [{bbox, confidence}].
    Tunables: HOG_DOWNSCALE_W / HOG_WINSTRIDE / HOG_SCALE (env)."""
    global _backend_status
    hog = _get_hog()
    if hog is None:
        _backend_status = "unavailable"
        return []
    try:
        # Downscale for speed; HOG wants ~64x128 windows
        h, w = frame.shape[:2]
        scale = HOG_DOWNSCALE_W / float(w) if w > HOG_DOWNSCALE_W else 1.0
        if scale < 1.0:
            frame_s = cv2.resize(frame, (int(w * scale), int(h * scale)))
        else:
            frame_s = frame
        rects, weights = None, None
        # HOG's detection window is 64x128 — handing it a smaller frame makes
        # OpenCV throw through the C++ boundary, which can corrupt the heap
        # (observed as 0xC0000374 on Windows). Guard the pipeline instead.
        if frame_s.shape[0] >= 128 and frame_s.shape[1] >= 64:
            rects, weights = hog.detectMultiScale(
                frame_s, winStride=(HOG_WINSTRIDE, HOG_WINSTRIDE),
                padding=(4, 4), scale=HOG_SCALE)
        else:
            logger.debug("HOG skipped: frame %dx%d below 64x128 window",
                         frame_s.shape[1], frame_s.shape[0])
            _backend_status = "hog"
            return []
        out = []
        for (x, y, rw, rh), weight in zip(rects, weights):
            conf = float(min(1.0, max(0.0, weight)))
            if conf < MIN_CONF:
                continue
            out.append({"bbox": [int(x / scale), int(y / scale),
                                 int(rw / scale), int(rh / scale)],
                        "confidence": round(conf, 3)})
        _backend_status = "hog"
        return out
    except Exception as exc:
        logger.error("HOG detection error: %s", exc)
        _backend_status = "failed"
        return []


def _detect_simulation(frame) -> list[dict]:
    """Simulation backend: never detects on its own; inject() drives it."""
    _backend_status = "simulation"
    return []


# ── YOLO backend (recommended over HOG) ──────────────────────────────────────

_yolo_sess = None                    # onnxruntime InferenceSession
_yolo_net = None                     # cv2.dnn.Net
_yolo_runtime: Optional[str] = None  # "onnxruntime" | "cv2.dnn" | None
_yolo_load_tried = False


def _yolo_runtime_name() -> Optional[str]:
    if _ORT_OK:
        return "onnxruntime"
    if _CV_OK:
        return "cv2.dnn"
    return None


def _yolo_model_available() -> bool:
    try:
        return os.path.isfile(YOLO_MODEL_PATH)
    except Exception:
        return False


def yolo_available() -> bool:
    """True when the ONNX model exists and a runtime can run it."""
    return _yolo_model_available() and _yolo_runtime_name() is not None


def _load_yolo() -> None:
    """Lazy, once. On any failure the YOLO backend is disabled and callers
    degrade (auto/hog fallback)."""
    global _yolo_sess, _yolo_net, _yolo_runtime, _yolo_load_tried
    if _yolo_load_tried:
        return
    _yolo_load_tried = True
    if not _yolo_model_available():
        logger.info("YOLO model not found at %s — yolo backend disabled "
                    "(export with: yolo export model=yolov8n.pt format=onnx "
                    "imgsz=320)", YOLO_MODEL_PATH)
        return
    try:
        if _ORT_OK:
            _yolo_sess = _ort.InferenceSession(
                YOLO_MODEL_PATH, providers=["CPUExecutionProvider"])
            _yolo_runtime = "onnxruntime"
        elif _CV_OK:
            _yolo_net = cv2.dnn.readNetFromONNX(YOLO_MODEL_PATH)
            _yolo_runtime = "cv2.dnn"
    except Exception as exc:
        _yolo_sess, _yolo_net, _yolo_runtime = None, None, None
        logger.warning("YOLO load failed (%s) — falling back to HOG", exc)


def _letterbox(frame, w: int, h: int):
    """Aspect-preserving resize + gray padding (YOLO convention). Returns
    (canvas, scale, pad_x, pad_y) so boxes can be mapped back exactly —
    fall evidence reads bbox aspect ratios, so stretching is not an option."""
    ih, iw = frame.shape[:2]
    scale = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = cv2.resize(frame, (nw, nh))
    canvas = np.full((h, w, 3), 114, dtype=np.uint8)
    dx, dy = (w - nw) // 2, (h - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, scale, dx, dy


def _detect_yolo(frame) -> list[dict]:
    """YOLO ONNX person detection (class 0 only). Letterboxes to a square
    YOLO_INPUT_PX input and decodes boxes back into original frame coords.
    Output layout matches ultralytics ONNX export: (1, 4+num_classes, N),
    center-format boxes, per-class scores, no objectness row. Any failure
    degrades to HOG when SENTRA_PERSON_YOLO_FALLBACK is enabled."""
    global _backend_status
    _load_yolo()
    if _yolo_runtime is None:
        return _detect_hog(frame) if YOLO_FALLBACK_TO_HOG else []
    try:
        canvas, scale, dx, dy = _letterbox(frame, YOLO_INPUT_PX, YOLO_INPUT_PX)
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0,
                                     (YOLO_INPUT_PX, YOLO_INPUT_PX),
                                     swapRB=True, crop=False)
        if _yolo_runtime == "onnxruntime":
            feeds = {i.name: blob for i in _yolo_sess.get_inputs()}
            out = _yolo_sess.run(None, feeds)[0]
        else:
            _yolo_net.setInput(blob)
            out = _yolo_net.forward()
        preds = np.asarray(out)[0]           # (84, N)
        if preds.ndim != 2 or preds.shape[0] < 5:
            raise ValueError(f"unexpected YOLO output shape {out.shape}")
        preds = preds.T                      # (N, 84)
        boxes_xywh = preds[:, :4]
        scores = preds[:, 4:]
        cls_ids = scores.argmax(axis=1)
        confs = scores.max(axis=1)

        conf_floor = max(YOLO_CONF, MIN_CONF)
        cands: list[tuple[list[int], float]] = []
        for (cx, cy, bw, bh), cid, conf in zip(boxes_xywh, cls_ids, confs):
            if int(cid) != 0:                # class 0 = person
                continue
            if conf < conf_floor:
                continue
            x = (float(cx) - float(bw) / 2.0 - dx) / scale
            y = (float(cy) - float(bh) / 2.0 - dy) / scale
            w = float(bw) / scale
            h = float(bh) / scale
            if w <= 1 or h <= 1:
                continue
            cands.append(([int(x), int(y), int(w), int(h)], float(conf)))

        if not cands:
            _backend_status = "yolo"
            return []
        keep = cv2.dnn.NMSBoxes([b for b, _ in cands], [c for _, c in cands],
                                YOLO_CONF, YOLO_NMS_IOU)
        detections = []
        for idx in np.asarray(keep).flatten():
            bbox, conf = cands[int(idx)]
            detections.append({"bbox": bbox, "confidence": round(conf, 3)})
        _backend_status = "yolo"
        return detections
    except Exception as exc:
        logger.error("YOLO detection error: %s", exc)
        if YOLO_FALLBACK_TO_HOG:
            return _detect_hog(frame)
        _backend_status = "failed"
        return []


_BACKENDS = {"hog": _detect_hog, "yolo": _detect_yolo,
             "simulation": _detect_simulation}


# ── Centroid tracker ─────────────────────────────────────────────────────────

def _bbox_center(bbox) -> tuple[float, float]:
    x, y, w, h = bbox
    return (x + w / 2.0, y + h / 2.0)


def _update_tracks(detections: list[dict], now: float, frame_id: str) -> dict[int, dict]:
    """Nearest-centroid matching with a max-displacement gate."""
    global _next_pid
    GATE_PX = 150.0  # persons move < ~1.5 m/frame at 10 Hz
    unmatched = list(detections)
    matched_ids = set()

    # Match by distance between previous and current centroids
    pairs = []
    for pid, tr in _tracks.items():
        c0 = _bbox_center(tr["bbox"])
        for i, det in enumerate(unmatched):
            c1 = _bbox_center(det["bbox"])
            dist = ((c0[0] - c1[0]) ** 2 + (c0[1] - c1[1]) ** 2) ** 0.5
            if dist < GATE_PX:
                pairs.append((dist, pid, i))
    pairs.sort()
    used_det = set()
    for dist, pid, i in pairs:
        if pid in matched_ids or i in used_det:
            continue
        det = unmatched[i]
        tr = _tracks[pid]
        tr["bbox"] = det["bbox"]
        tr["confidence"] = det["confidence"]
        tr["last_seen"] = now
        tr["frames"] += 1
        tr["frame_id"] = frame_id
        matched_ids.add(pid)
        used_det.add(i)

    # New persons
    for i, det in enumerate(unmatched):
        if i in used_det:
            continue
        pid = _next_pid
        _next_pid += 1
        _tracks[pid] = {"bbox": det["bbox"], "confidence": det["confidence"],
                        "last_seen": now, "frames": 1, "frame_id": frame_id}
        matched_ids.add(pid)

    # Expire stale tracks
    for pid in list(_tracks.keys()):
        if now - _tracks[pid]["last_seen"] > MAX_AGE_S:
            del _tracks[pid]

    return {pid: dict(tr) for pid, tr in _tracks.items()}


# ── Public API ───────────────────────────────────────────────────────────────

def inject(persons: int = 1, confidence: float = 0.92) -> dict:
    """Simulation hook: inject N persons as if detected in the newest frame."""
    now = time.time()
    frame_id = f"sim-{int(now * 1000)}"
    detections = []
    for i in range(persons):
        x = 200 + i * 250
        detections.append({"bbox": [x, 120, 160, 360], "confidence": confidence})
    _process_detections(detections, frame_id, source="simulation")
    return {"injected": persons, "frame_id": frame_id}


def _process_detections(detections: list[dict], frame_id: str,
                        source: str = "phone") -> None:
    global _updates_since_event
    now = time.time()
    with _lock:
        tracks = _update_tracks(detections, now, frame_id)
        for pid, tr in tracks.items():
            entry = {
                "person_id": pid,
                "bbox": tr["bbox"],
                "confidence": tr["confidence"],
                "timestamp": tr["last_seen"],
                "frame_id": tr["frame_id"],
                "source": source,
            }
            _history.append(entry)
        n = len(tracks)
        if n > 0:
            _updates_since_event += 1
            should_event = _updates_since_event >= EVENT_EVERY
            if should_event:
                _updates_since_event = 0
    if n > 0 and should_event:
        try:
            safety_events.report("PERSON", {
                "persons": n,
                "person_ids": sorted(tracks.keys()),
            }, severity="INFO")
        except Exception:
            pass


def _on_frame(frame, frame_id: str) -> None:
    """Vision subscriber: queue the newest frame for the worker."""
    try:
        if len(_queue) == _queue.maxlen:
            _queue.popleft()
        _queue.append((frame, frame_id))
    except Exception:
        pass


def resolve_backend_name() -> str:
    """'auto' prefers yolo when the ONNX model + a runtime exist (recommended:
    far better accuracy than HOG at similar CPU cost), else hog when OpenCV
    is importable, else simulation. Explicit names are honored with the same
    degradation chain yolo→hog→simulation."""
    if PERSON_BACKEND == "simulation":
        return "simulation"
    if PERSON_BACKEND == "yolo":
        return "yolo" if yolo_available() else ("hog" if _CV_OK else "simulation")
    if PERSON_BACKEND == "hog":
        return "hog" if _CV_OK else "simulation"
    if yolo_available():                    # auto
        return "yolo"
    return "hog" if _CV_OK else "simulation"


def _worker_loop() -> None:
    global _frames_seen, _resolved_backend, _last_detect_ms, _detect_ms_ema
    name = resolve_backend_name()
    backend = _BACKENDS.get(name, _detect_simulation)
    _resolved_backend = name
    logger.info("Person detection worker started (backend=%s requested=%s, poll=%.2fs)",
                name, PERSON_BACKEND, POLL_S)
    while not _stop_event.is_set():
        try:
            frame, frame_id = _queue.popleft()
        except IndexError:
            _stop_event.wait(POLL_S)
            continue
        _frames_seen += 1
        t0 = time.perf_counter()
        detections = backend(frame)
        ms = (time.perf_counter() - t0) * 1000.0
        _last_detect_ms = ms
        _detect_ms_ema = ms if _detect_ms_ema is None \
            else (0.7 * _detect_ms_ema + 0.3 * ms)
        _process_detections(detections, frame_id)
        _stop_event.wait(POLL_S)
    logger.info("Person detection worker stopped")


def start() -> None:
    """Idempotent; call from lifespan. Registers the vision subscriber."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not PERSON_ENABLED:
        logger.info("Person detection disabled by config")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker_loop, name="sentra-person", daemon=True)
    _thread.start()
    from services import vision_service
    vision_service.subscribe(_on_frame, name="person_detection")
    logger.info("Person detection subscribed to vision hub")


def stop() -> None:
    _stop_event.set()


def get_detections(limit: int = 20) -> list[dict]:
    with _lock:
        items = list(_history)[-limit:]
    return items[::-1]  # newest first


def get_tracked() -> list[dict]:
    """Currently-tracked persons with staleness age."""
    now = time.time()
    with _lock:
        out = []
        for pid, tr in _tracks.items():
            d = dict(tr)
            d["person_id"] = pid
            d["age_s"] = round(now - tr["last_seen"], 2)
            out.append(d)
    out.sort(key=lambda d: d["person_id"])
    return out


def stats() -> dict:
    _load_yolo()  # populate _yolo_runtime so stats reflect real availability
    with _lock:
        return {
            "enabled": PERSON_ENABLED,
            "backend": _backend_status,
            "resolved_backend": _resolved_backend,
            "requested_backend": PERSON_BACKEND,
            "opencv_available": _CV_OK,
            "yolo_available": yolo_available(),
            "yolo_model_path": YOLO_MODEL_PATH,
            "yolo_runtime": _yolo_runtime,
            "frames_seen": _frames_seen,
            "tracked_count": len(_tracks),
            "history_size": len(_history),
            "min_confidence": MIN_CONF,
            # Phase 6: detection latency visibility (benchmark on the Pi via
            # these numbers; tune SENTRA_PERSON_HOG_* accordingly).
            "avg_detect_ms": (round(_detect_ms_ema, 1)
                              if _detect_ms_ema is not None else None),
            "last_detect_ms": (round(_last_detect_ms, 1)
                               if _last_detect_ms is not None else None),
            "hog_downscale_width": HOG_DOWNSCALE_W,
            "hog_winstride": HOG_WINSTRIDE,
        }
