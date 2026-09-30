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
from services.connection_manager import connection_manager


# Routers (REST)
from routers import system, auth, pair, telemetry, camera, control, alerts, settings as settings_router, call
from routers import manual_control
from routers import safety as safety_router
from routers import localization as localization_router
from routers import mapping as mapping_router
from routers import patrol as patrol_router
from routers import docking as docking_router
from routers import voice as voice_router
from routers import person as person_router
from routers import person_registry as person_registry_router
from routers import fall as fall_router
from routers import emergency as emergency_router
from routers import notifications as notifications_router
from routers import devices as devices_router
from routers import relay as relay_router
from routers import simulation as simulation_router  # Phase 18
from routers import compat as compat_router  # Phase 21: final app spec aliases
from routers import route_graph as route_graph_router  # Phase 5: taught route graph

# WebSocket handlers
from ws_handlers import telemetry_ws, control_ws, alerts_ws, call_ws
from ws_handlers import compat_ws  # Phase 21: multiplexed app socket
from ws_handlers import app_ws  # Phase 1 contract: /ws/user + /ws/rover

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
    from core import simulation as sim_mode  # Phase 18: mode is fixed at import
    logger.info("═══════════════════════════════════════════════")
    logger.info("  SENTRA Backend starting on %s:%d", host_ip, settings.port)
    logger.info("  Unit: %s  (%s)", settings.unit_name, settings.unit_id)
    logger.info("  Mode: %s", sim_mode.describe())  # Phase 18
    logger.info("═══════════════════════════════════════════════")

    # ── Phase 18: explicit simulation mode — preflight stop + loud banner ──
    sim_mode.preflight_stop()
    sim_mode.banner()

    # ── Phase 20: startup security checks (log findings, never abort) ─────
    from core import hardening
    hardening.run_checks()

    # Start UDP broadcast discovery responder
    from services.udp_discovery import start_udp_discovery  # Phase 0 fix: import lost
    asyncio.create_task(start_udp_discovery(host_ip))

    # ── Phase 2: Centralized Hardware Lifecycle ──────────────────────
    from hardware.manager import hardware_manager
    hardware_manager.initialize()

    # ── Phase 1: wire the centralized safety layer ──────────────────────
    from core.safety import get_safety_layer
    from services import motor_service

    # ── Phase 4: unified sensor aggregator feeds the safety gate ────
    from services import sensor_service
    sensor_service.start()

    from services import imu_service
    imu_service.start_monitoring()
    
    from services import current_service
    current_service.start()

    safety = get_safety_layer()
    safety.wire(motor_apply=motor_service._apply_wheel_duty,
                sensor_provider=sensor_service.sensor_provider)
    safety.start_watchdog()

    # ── Phase 3: safety events → alerts WS bridge ──────────────────────
    from services import safety_events
    safety.wire_event_reporter(safety_events.report)

    # ── Phase 2: Cliff monitoring ──────────────────────
    from services import cliff_service
    cliff_service.start_monitoring()

    # ── Phase 14: notifications (mirror safety events → persistent feed) ──
    from services import notification_service
    notification_service.load()   # ~/sentra_data/notifications.json
    notification_service.attach_loop(asyncio.get_running_loop())
    safety_events.add_listener(notification_service.notify_safety_event)

    # ── Phase 4: Start mapping recording loop ──────────────────────────
    from services import mapping_service
    asyncio.create_task(mapping_service.recording_loop())

    # ── Phase 15: device registry + JWT auth enforcement ────────────────
    from services import device_registry
    device_registry.load()   # ~/sentra_data/devices.json

    # ── Phase 16: cloud relay (remote access; disabled without SENTRA_RELAY_URL) ──
    from services import relay_client
    relay_client.configure_from_env()
    relay_client.attach_loop(asyncio.get_running_loop())
    safety_events.add_listener(relay_client._on_safety_event)  # DANGER push
    # ── Phase 21: final app spec socket — alert/emergency instant push ──
    from ws_handlers import compat_ws
    safety_events.add_listener(compat_ws._on_safety_event)
    relay_client.start()
    safety_events.attach_loop(asyncio.get_running_loop())
    cliff_service.start_monitoring()

    # Patrol thread starts idle; it only acts while mode == PATROL
    motor_service.start_patrol_loop()

    # ── Phase 7: patrol routes + engine ──────────────────────────
    from services import patrol_service
    patrol_service.load_routes()   # loads ~/sentra_data/patrol_routes.json if present
    patrol_service.start_engine()

    # ── Phase 8: return-to-dock engine ────────────────────────────
    from services import docking_service
    docking_service.start_engine()

    # ── Phase 9: navigation (go-to-location) engine ──────────────
    from services import navigation_service
    from services import route_graph
    route_graph.load()  # taught route graph (Phase 5: P11)
    navigation_service.start_engine()

    # ── Phase 5+6: vision pipeline (phone frames → AprilTag → location/map) ──
    from services import tag_map, vision_service, apriltag_service, mapping_service
    tag_map.load()  # creates data/tag_map.json with defaults on first run
    vision_service.subscribe(
        lambda frame, frame_id: apriltag_service.process_frame(frame, frame_id),
        name="apriltag")
    # Phase 0 repair: the old subscription called mapping_service.on_frame
    # before that method existed. The method now exists on MappingService and
    # is subscribed again — remove this block only if the mapping feature is
    # intentionally dropped.
    vision_service.subscribe(mapping_service.on_frame, name="mapping")

    # ── Phase 10: person detection (phone frames → tracked persons) ────
    from services import person_detection
    person_detection.start()   # subscribes to vision hub internally

    # ── Phase 11: person recognition (faces → known/unknown + alerts) ────
    from services import person_registry, person_recognition
    person_registry.load()     # data/person_registry.json
    person_recognition.start()  # subscribes to vision hub internally

    # ── Phase 12: fall detection (temporal evidence → FALL_CONFIRMED) ────
    from services import fall_detection
    fall_detection.start()  # sampler consumes Phase 10 tracked persons

    # ── Phase 13: emergency auto-call (fall → invite caregiver app) ──────
    from services import emergency_call
    emergency_call.attach_loop(asyncio.get_running_loop())
    emergency_call.register_with_fall()  # hook: on_fall_confirmed
    vision_service.start()

    # ── Phase 2: start the motion controller (MANUAL authority) ───────
    from services.motion_controller import get_motion_controller
    motion = get_motion_controller()
    motion.start()

    # Ensure snapshot directory exists
    os.makedirs(settings.camera_snapshot_dir, exist_ok=True)

    # ── Phase 20: systemd integration — watchdog heartbeat + READY=1 ─────
    # No-ops when not running under systemd (Type=notify + WatchdogSec).
    from core import sd_notify
    sd_notify.heartbeat.start()
    sd_notify.notify("READY=1")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("SENTRA Backend shutting down")
    from core import sd_notify as _sd  # Phase 20
    _sd.notify("STOPPING=1")
    _sd.heartbeat.stop()

    # HAL Shutdown
    from hardware.manager import hardware_manager
    hardware_manager.shutdown()

    from services import sensor_service
    from services import vision_service
    mapping_service.stop_session()
    patrol_service.stop_patrol()
    patrol_service.stop_engine()
    docking_service.cancel_return("shutdown")
    docking_service.stop_engine()
    navigation_service.cancel("shutdown")
    navigation_service.stop_engine()
    person_detection.stop()
    person_recognition.stop()
    relay_client.stop()
    fall_detection.stop()
    motion.shutdown()
    motor_service.stop_patrol_loop()
    motor_service.stop_all("shutdown")
    sensor_service.stop()
    current_service.stop()


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
# ── Phase 15: bearer auth on mutating REST endpoints (staged rollout) ────────
# Read-only endpoints and the login/pair/docs paths stay open; /ws/control is
# gated inside its own handler. SENTRA_AUTH_ENFORCED=false disables (dev).
from fastapi import HTTPException as _HTTPException
from starlette.responses import JSONResponse as _JSONResponse
from core import auth as _core_auth


@app.middleware("http")
async def _auth_enforcement(request, call_next):
    try:
        await _core_auth.enforce_auth(request)
    except _HTTPException as exc:
        return _JSONResponse(status_code=exc.status_code,
                             content={"detail": exc.detail})
    return await call_next(request)


# CORS — Phase 4 hardening: browsers must not be able to pair a wildcard
# origin with credentialed requests. The Flutter app (non-web) and LAN tools
# send no Origin header at all, so the default here does not restrict them;
# a browser-based client must come from an allowed origin. Add
# SENTRA_CORS_ORIGINS=https://app.example.com to restrict further.
import os as _os
_origins_env = _os.getenv("SENTRA_CORS_ORIGINS", "").strip()
if _origins_env:
    _allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    _allow_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=False,
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
app.include_router(manual_control.router)  # Phase 2: /api/v1/control/*
app.include_router(safety_router.router)   # Phase 3: /api/v1/safety/*
app.include_router(localization_router.router)  # Phase 5: /api/v1/localization/*
app.include_router(mapping_router.router)       # Phase 6: /api/v1/map/*
app.include_router(patrol_router.router)        # Phase 7: /api/v1/patrol/*
app.include_router(docking_router.router)       # Phase 8: /api/v1/dock/*
app.include_router(voice_router.router)         # Phase 9: /api/v1/voice/*
app.include_router(person_router.router)        # Phase 10: /api/v1/person/*
app.include_router(person_registry_router.router)  # Phase 11: /api/v1/persons/*
app.include_router(fall_router.router)
app.include_router(emergency_router.router)  # Phase 13: /api/v1/emergency/*
app.include_router(notifications_router.router)  # Phase 14: /api/v1/notifications/*
app.include_router(devices_router.router)  # Phase 15: /api/v1/devices/*
app.include_router(relay_router.router)  # Phase 16: /api/v1/relay/*            # Phase 12: /api/v1/fall/*
app.include_router(simulation_router.router)  # Phase 18: /api/v1/simulation/*
app.include_router(compat_router.router)  # Phase 21: /api/* final app spec
app.include_router(route_graph_router.router)  # Phase 5: /api/v1/nav/graph

# ── Mount WebSocket routers ───────────────────────────────────────────────────
app.include_router(telemetry_ws.router)
app.include_router(control_ws.router)
app.include_router(alerts_ws.router)
app.include_router(call_ws.router)
app.include_router(compat_ws.router)  # Phase 21: /ws multiplexed app socket
app.include_router(app_ws.router)  # Phase 1 contract: /ws/user + /ws/rover

# ── Static file serving (snapshots download) ──────────────────────────────────
_snapshot_dir = settings.camera_snapshot_dir
os.makedirs(_snapshot_dir, exist_ok=True)
app.mount("/snapshots", StaticFiles(directory=_snapshot_dir), name="snapshots")

# ── Phase 3: person-recognition snapshots at /media ─────────────────────────
# person_object() / alert_object() emit /media/<file> image URLs; until now
# nothing served that prefix (audit P-bug: image_url 404).
from services.person_recognition import SNAPSHOT_DIR as _media_dir
os.makedirs(_media_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=_media_dir), name="media")


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
