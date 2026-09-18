import asyncio
import io
import time
import logging
from typing import Optional, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

# Buffers
node_frame: Optional[bytes] = None
user_frame: Optional[bytes] = None
node_last_seen: float = 0.0
user_last_seen: float = 0.0

call_clients: dict[str, set[WebSocket]] = {"node": set(), "user": set()}
webrtc_clients: dict[str, set[WebSocket]] = {"node": set(), "user": set()}

MJPEG_BOUNDARY = b"frame"
MAX_FRAME_BYTES = 5 * 1024 * 1024


def is_valid_jpeg(data: bytes) -> bool:
    if data is None:
        return False
    return (
        4 <= len(data) <= MAX_FRAME_BYTES
        and data[:2] == b"\xff\xd8"
        and data[-2:] == b"\xff\xd9"
    )


async def broadcast_frame(role: str, frame: bytes) -> None:
    disconnected: list[WebSocket] = []
    for client in tuple(call_clients[role]):
        try:
            await client.send_bytes(frame)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        call_clients[role].discard(client)


async def broadcast_signal(role: str, message: dict) -> None:
    """Forward WebRTC signaling JSON; media never passes through this server."""
    disconnected: list[WebSocket] = []
    for client in tuple(webrtc_clients[role]):
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)
    for client in disconnected:
        webrtc_clients[role].discard(client)


def process_frame(data: bytes, max_width: int = 640) -> bytes:
    if Image is None:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
        if exif:
            orientation = exif.get(274, 1)
            rotations = {3: 180, 6: 270, 8: 90}
            if orientation in rotations:
                img = img.rotate(rotations[orientation], expand=True)
        img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=55, optimize=False)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Error processing frame: {e}")
        return data


def generate_placeholder_frame(text: str) -> bytes:
    if Image is None:
        return b""
    img = Image.new("RGB", (640, 480), color=(15, 20, 35))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except Exception:
        font = ImageFont.load_default()
    
    # Simple text centering
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((640 - w) / 2, (480 - h) / 2), text, fill=(100, 120, 160), font=font)
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


# Initialize with placeholders
node_frame = generate_placeholder_frame("Awaiting Node (Phone A) feed...")
user_frame = generate_placeholder_frame("Awaiting User (Phone B) feed...")


async def update_node_frame(raw_bytes: bytes):
    global node_frame, node_last_seen
    node_frame = process_frame(raw_bytes)
    node_last_seen = time.time()
    await broadcast_frame("user", node_frame) # Broadcast to peer


async def update_user_frame(raw_bytes: bytes):
    global user_frame, user_last_seen
    user_frame = process_frame(raw_bytes)
    user_last_seen = time.time()
    await broadcast_frame("node", user_frame) # Broadcast to peer


def mjpeg_generator(get_frame_fn):
    while True:
        frame = get_frame_fn()
        if not frame:
            time.sleep(0.1)
            continue
            
        header = (
            b"--" + MJPEG_BOUNDARY + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
            b"\r\n"
        )
        yield header + frame + b"\r\n"
        time.sleep(1 / 30)
