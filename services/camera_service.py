"""
SENTRA — MJPEG / Camera service.

On Raspberry Pi 5 this uses Picamera2 (preferred) and falls back to
OpenCV (USB webcam / dev machine).  The generator yields MJPEG frames
suitable for FastAPI's StreamingResponse.
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# ── IR Night Vision state ─────────────────────────────────────────────────────

_ir_active = False


def toggle_ir_filter(enabled: bool) -> dict:
    global _ir_active
    _ir_active = enabled
    mode = "IR_NIGHT_VISION" if enabled else "STANDARD"
    logger.info("IR filter → %s", mode)
    # On Pi: GPIO.output(IR_RELAY_PIN, GPIO.HIGH if enabled else GPIO.LOW)
    return {"ir_filter_active": _ir_active, "mode": mode}


# ── MJPEG stream generator ────────────────────────────────────────────────────

def _try_picamera2():
    """Attempt to import and initialise Picamera2 (Pi only)."""
    try:
        from picamera2 import Picamera2  # type: ignore
        cam = Picamera2()
        cam.configure(cam.create_video_configuration(main={"size": (1280, 720)}))
        cam.start()
        return cam, "picamera2"
    except Exception as e:
        logger.error(f"Picamera2 failed to initialize: {e}", exc_info=True)
        return None, None


def _try_opencv():
    """Attempt to open an OpenCV VideoCapture (USB cam / dev)."""
    try:
        import cv2  # type: ignore
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            return cap, "opencv"
        cap.release()
    except Exception:
        pass
    return None, None


def mjpeg_frame_generator(quality: int = 80):
    """
    Generator that yields raw MJPEG multipart frames.
    Consumed by FastAPI StreamingResponse.
    """
    cam, backend = _try_picamera2()
    if cam is None:
        cam, backend = _try_opencv()

    logger.info("Camera backend: %s", backend or "synthetic")

    try:
        while True:
            frame_bytes = None

            if backend == "picamera2":
                import cv2  # type: ignore
                import numpy as np
                arr = cam.capture_array()
                _, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, quality])
                frame_bytes = buf.tobytes()

            elif backend == "opencv":
                import cv2  # type: ignore
                ret, frame = cam.read()
                if not ret:
                    break
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                frame_bytes = buf.tobytes()

            else:
                # Synthetic grey frame for headless development
                import cv2  # type: ignore
                import numpy as np
                synthetic = np.full((480, 640, 3), 30, dtype=np.uint8)
                cv2.putText(
                    synthetic, f"SENTRA CAMERA — NO DEVICE  {time.strftime('%H:%M:%S')}",
                    (30, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
                )
                _, buf = cv2.imencode(".jpg", synthetic, [cv2.IMWRITE_JPEG_QUALITY, quality])
                frame_bytes = buf.tobytes()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
            )

    finally:
        if backend == "picamera2":
            cam.stop()
        elif backend == "opencv":
            cam.release()


# ── Snapshot ──────────────────────────────────────────────────────────────────

def capture_snapshot(snapshot_dir: str, host_ip: str, port: int) -> dict:
    """
    Save a single JPEG snapshot and return its path + download URL.
    """
    import cv2  # type: ignore
    import numpy as np

    os.makedirs(snapshot_dir, exist_ok=True)
    filename = f"snap_{int(time.time())}.jpg"
    filepath = os.path.join(snapshot_dir, filename)

    cam, backend = _try_picamera2()
    if cam is None:
        cam, backend = _try_opencv()

    if backend == "picamera2":
        arr = cam.capture_array()
        import cv2
        cv2.imwrite(filepath, arr)
        cam.stop()
    elif backend == "opencv":
        ret, frame = cam.read()
        cam.release()
        if ret:
            cv2.imwrite(filepath, frame)
    else:
        # Synthetic snapshot
        img = np.full((1080, 1920, 3), 30, dtype=np.uint8)
        cv2.putText(img, "SENTRA SNAPSHOT", (600, 540),
                    cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 255, 0), 5)
        cv2.imwrite(filepath, img)

    download_url = f"http://{host_ip}:{port}/snapshots/{filename}"
    return {"file_path": filepath, "download_url": download_url}
