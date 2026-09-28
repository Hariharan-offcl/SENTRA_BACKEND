from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from threading import Lock

from core import config as core_config
from core.state import get_robot_state, MANUAL
from services import sensor_service, tag_map

logger = logging.getLogger(__name__)

@dataclass
class MappingPoint:
    timestamp: float
    x: float
    y: float
    heading: float
    tag_id: Optional[int]
    room_name: Optional[str]
    battery_pct: float
    current_ma: float
    health: str

class MappingService:
    """
    Handles the manual mapping sessions where a user drives the robot
    to teach it locations and record an odometry trace.
    """
    def __init__(self):
        self._lock = Lock()
        self._active = False
        self._session_id: Optional[str] = None
        self._file_handle = None

        # Current local coordinates
        self._x = 0.0
        self._y = 0.0
        self._heading = 0.0

        # Buffer for high-frequency recording to avoid blocking on I/O
        self._buffer = deque(maxlen=100)
        self._recording_task: Optional[asyncio.Task] = None

    def start_session(self) -> dict:
        """Starts a mapping session and resets the local origin."""
        st = get_robot_state()
        if st.get_mode() != MANUAL:
            # Auto-request MANUAL mode for mapping
            result = st.request_mode(MANUAL, requested_by="mapping_service")
            if not result.get("accepted"):
                return {"ok": False, "error": "could not enter MANUAL mode"}

        with self._lock:
            self._active = True
            self._x = 0.0
            self._y = 0.0
            self._heading = 0.0
            self._session_id = f"session_{int(time.time())}"

            # Open JSONL file for appending
            path = os.path.join(core_config.SENTRA_MAP_SNAPSHOT_DIR, f"{self._session_id}_trace.jsonl")
            os.makedirs(core_config.SENTRA_MAP_SNAPSHOT_DIR, exist_ok=True)
            self._file_handle = open(path, "a")

            logger.info("Mapping session started: %s. Origin set to (0,0,0)", self._session_id)

        return {"ok": True, "session_id": self._session_id, "file": path}

    def stop_session(self) -> dict:
        """Stops recording and closes the file."""
        with self._lock:
            if not self._active:
                return {"ok": False, "error": "no active session"}

            self._active = False
            if self._file_handle:
                self._file_handle.close()
                self._file_handle = None

            session_id = self._session_id
            self._session_id = None
            logger.info("Mapping session %s stopped", session_id)

        return {"ok": True, "session_id": session_id}

    def update_odometry(self, delta_dist: float, delta_heading: float):
        """
        Integrates wheel distance and IMU yaw to update local (x, y, theta).
        Called by the sensor aggregator or a dedicated loop.
        """
        with self._lock:
            if not self._active:
                return

            # Simple 2D dead reckoning
            import math
            rad = math.radians(self._heading)
            self._x += delta_dist * math.cos(rad)
            self._y += delta_dist * math.sin(rad)
            self._heading = (self._heading + delta_heading) % 360.0

    async def recording_loop(self):
        """
        Background loop that captures 10Hz snapshots and flushes to disk.
        """
        while True:
            if self._active:
                try:
                    # Capture snapshot
                    snap = sensor_service.get_snapshot()
                    imu = snap.get("imu", {})
                    enc = snap.get("wheel_encoders", {})

                    # Get current tag/room
                    # Note: In a real implementation, this would query localization_service
                    tag_id = None
                    room_name = "unknown"
                    # Placeholder: logic to find best tag from sensor_service/apriltag_service

                    # Get battery/current from telemetry
                    from services.telemetry_service import _sim
                    battery = _sim.get("battery", {}).get("pct", 0.0)
                    current = _sim.get("battery", {}).get("current_ma", 0.0)

                    point = MappingPoint(
                        timestamp=time.time(),
                        x=self._x,
                        y=self._y,
                        heading=self._heading,
                        tag_id=tag_id,
                        room_name=room_name,
                        battery_pct=battery,
                        current_ma=current,
                        health="OK" # Simplified
                    )

                    # Append to file
                    if self._file_handle:
                        self._file_handle.write(json.dumps(asdict(point)) + "\n")
                        # Flush occasionally or use a buffer. For JSONL, write is usually fast.
                except Exception as e:
                    logger.error("Mapping record error: %s", e)

            await asyncio.sleep(0.1) # 10 Hz

    def get_current_position(self) -> dict:
        with self._lock:
            return {
                "x": self._x,
                "y": self._y,
                "heading": self._heading,
                "active": self._active
            }

# Singleton instance
mapping_service = MappingService()
