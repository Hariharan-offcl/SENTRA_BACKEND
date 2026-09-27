"""
SENTRA — Vision service (Phase 5).

The phone-mounted camera is the ONLY vision source. Frames arrive at the
backend through the existing video-call path (call_service: /ws/call/{role}
binary frames or /api/v1/call/upload-node HTTP), and the vision service taps
that stream here — without disturbing the call feature.

Design:
    - A background sampler pulls the latest 'node' (rover-phone) frame at
      ~10 Hz and pushes it to every subscriber's bounded queue (drop-oldest
      backpressure — vision consumers must never slow the call path).
    - Subscribers are plain callables: apriltag_service.process_frame today;
      person/fall detection plug in the same way in later phases.
    - When no phone is streaming, everything stays idle and simulated.

Call-path note: update_node_frame() currently encodes the JPEG into a Pillow
image and DISCARDS it; Phase 5 hooks the raw JPEG into this service instead,
so vision and the video call share one ingress without code changes in the
Flutter app.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from typing import Callable

logger = logging.getLogger(__name__)

SAMPLER_HZ = 10.0
QUEUE_MAX = 2  # per subscriber — drop-oldest backpressure

_subscribers: list[dict] = []          # {"queue": deque, "callback": fn, "name": str}
_subs_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None
_stats = {
    "frames_ingressed": 0,
    "frames_sampled": 0,
    "last_frame_age_s": None,
    "running": False,
}
_stats_lock = threading.Lock()


def subscribe(callback: Callable[[object, str], None], name: str) -> None:
    """Register a frame consumer. Callback signature: (frame, frame_id).
    Frames are delivered from the sampler thread — keep callbacks fast and
    never blocking (detection services own their threading internally)."""
    with _subs_lock:
        _subscribers.append({"queue": deque(maxlen=QUEUE_MAX), "callback": callback, "name": name})
    logger.info("Vision subscriber registered: %s", name)


def unsubscribe(name: str) -> None:
    with _subs_lock:
        _subscribers[:] = [s for s in _subscribers if s["name"] != name]


def _latest_node_jpeg() -> bytes | None:
    """Latest raw JPEG from the rover phone via the call path."""
    from services import call_service
    frame = call_service.node_frame
    if frame and (time.time() - call_service.node_last_seen) < 5.0:
        return frame
    return None


def _decode(frame_bytes: bytes):
    import cv2
    import numpy as np
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def _sampler_loop() -> None:
    while not _stop_event.is_set():
        jpeg = _latest_node_jpeg()
        if jpeg is None:
            _stop_event.wait(0.25)
            continue
        try:
            frame = _decode(jpeg)
        except Exception as exc:
            logger.error("Vision frame decode error: %s", exc)
            _stop_event.wait(0.25)
            continue
        if frame is None:
            _stop_event.wait(0.05)
            continue

        frame_id = f"f{int(time.time() * 1000)}"
        with _stats_lock:
            _stats["frames_ingressed"] += 1
            _stats["last_frame_age_s"] = 0.0

        with _subs_lock:
            subs = list(_subscribers)
        for sub in subs:
            q = sub["queue"]
            if len(q) == q.maxlen:
                try:
                    q.popleft()  # drop oldest
                except IndexError:
                    pass
            q.append((frame, frame_id))

        # Drain queues → callbacks (from sampler thread; consumers must be fast)
        for sub in subs:
            q = sub["queue"]
            while q:
                try:
                    frame_item, fid = q.popleft()
                except IndexError:
                    break
                try:
                    sub["callback"](frame_item, fid)
                except Exception as exc:
                    logger.error("Vision subscriber '%s' failed: %s", sub["name"], exc)

        with _stats_lock:
            _stats["frames_sampled"] += 1
        _stop_event.wait(1.0 / SAMPLER_HZ)


def start() -> None:
    """Idempotent; call from lifespan."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_sampler_loop, name="sentra-vision", daemon=True)
    _thread.start()
    with _stats_lock:
        _stats["running"] = True
    logger.info("Vision sampler started (%.0f Hz, %d max queued per subscriber)",
                SAMPLER_HZ, QUEUE_MAX)


def stop() -> None:
    _stop_event.set()
    with _stats_lock:
        _stats["running"] = False


def stats() -> dict:
    with _stats_lock:
        s = dict(_stats)
    with _subs_lock:
        s["subscribers"] = [sub["name"] for sub in _subscribers]
    from services import call_service
    s["phone_streaming"] = (time.time() - call_service.node_last_seen) < 5.0
    return s
