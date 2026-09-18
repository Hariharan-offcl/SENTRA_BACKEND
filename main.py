"""
SENTRA — Main FastAPI application entry-point.

Run on Raspberry Pi 5:
    python3 main.py

Or with uvicorn directly:
    uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import logging
import socket
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from services.udp_discovery import start_udp_discovery

# Routers (REST)
from routers import system, auth, pair, telemetry, camera, control, alerts, settings as settings_router, call

# WebSocket handlers
from ws_handlers import telemetry_ws, control_ws, alerts_ws, call_ws

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sentra.main")


# ── Lifespan (startup / shutdown hooks) ───────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    host_ip = _get_local_ip()
    logger.info("═══════════════════════════════════════════════")
    logger.info("  SENTRA Backend starting on %s:%d", host_ip, settings.port)
    logger.info("  Unit: %s  (%s)", settings.unit_name, settings.unit_id)
    logger.info("═══════════════════════════════════════════════")

    # Start UDP broadcast discovery responder
    asyncio.create_task(start_udp_discovery(host_ip))

    # Ensure snapshot directory exists
    os.makedirs(settings.camera_snapshot_dir, exist_ok=True)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("SENTRA Backend shutting down")


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="SENTRA Pi 5 Gateway API",
    description=(
        "Raspberry Pi 5 backend for the SENTRA Companion Robotics & Defense "
        "Mobile Application. Provides real-time telemetry, MJPEG video, "
        "motor control, and safety alert WebSockets."
    ),
    version=settings.api_version,
    lifespan=lifespan,
)

# CORS — allow the Flutter app to reach the Pi from any origin on the LAN
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount REST routers ────────────────────────────────────────────────────────
app.include_router(system.router)
app.include_router(auth.router)
app.include_router(pair.router)
app.include_router(telemetry.router)
app.include_router(camera.router)
app.include_router(control.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
app.include_router(call.router)

# ── Mount WebSocket routers ───────────────────────────────────────────────────
app.include_router(telemetry_ws.router)
app.include_router(control_ws.router)
app.include_router(alerts_ws.router)
app.include_router(call_ws.router)

# ── Static file serving (snapshots download) ──────────────────────────────────
_snapshot_dir = settings.camera_snapshot_dir
os.makedirs(_snapshot_dir, exist_ok=True)
app.mount("/snapshots", StaticFiles(directory=_snapshot_dir), name="snapshots")


# ── Utility ───────────────────────────────────────────────────────────────────

def _get_local_ip() -> str:
    """Determine the Pi's LAN IP by opening a throwaway UDP socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
