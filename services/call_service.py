import time
import io
import logging

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

# Buffers
node_frame_buffer: bytes = b""
node_last_seen: float = 0.0

user_frame_buffer: bytes = b""
user_last_seen: float = 0.0


def generate_placeholder_frame(title: str, subtitle: str) -> bytes:
    if Image is None:
        return b""
    try:
        img = Image.new("RGB", (640, 480), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.text((180, 210), title, fill=(0, 255, 170))
        draw.text((160, 240), subtitle, fill=(180, 190, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Failed to generate placeholder: {e}")
        return b""


# Initialize with placeholders
node_frame_buffer = generate_placeholder_frame(
    "SENTRA PI 5 MEDIA GATEWAY", "Awaiting Phone A (Node) Video Feed..."
)
user_frame_buffer = generate_placeholder_frame(
    "SENTRA 2-WAY CALL GATEWAY", "Awaiting Phone B (User) Video Feed..."
)


def update_node_frame(frame_bytes: bytes):
    global node_frame_buffer, node_last_seen
    node_frame_buffer = frame_bytes
    node_last_seen = time.time()


def update_user_frame(frame_bytes: bytes):
    global user_frame_buffer, user_last_seen
    user_frame_buffer = frame_bytes
    user_last_seen = time.time()


def stream_generator(target: str):
    """
    Yields frames continuously for MJPEG streaming.
    target: 'node' or 'user'
    """
    global node_frame_buffer, user_frame_buffer
    
    while True:
        if target == 'node':
            frame = node_frame_buffer
        else:
            frame = user_frame_buffer
            
        if not frame:
            time.sleep(0.1)
            continue
            
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        )
        # Yield at ~30 FPS max
        time.sleep(0.03)
