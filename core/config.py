"""
SENTRA — Core runtime configuration.

Safety thresholds and switches that the safety layer reads. Unlike the global
`config.py` (unit identity / network), these values are about robot behaviour
and may be updated at runtime later (settings API, Phase 3+).

Everything here is hardware-independent: on a dev machine with no sensors the
same thresholds apply, they just never trigger.
"""

from __future__ import annotations

import os

# ── Simulation ────────────────────────────────────────────────────────────────
# SENTRA_SIMULATION=true → fake sensors, GPIO writes become no-ops.
# Phase 18 will expand this; the flag exists from day one so the safety layer
# can behave identically in both modes.
SIMULATION = os.getenv("SENTRA_SIMULATION", "false").strip().lower() in ("1", "true", "yes", "on")

# ── Safety thresholds (Phase 3 will expose these via settings API) ───────────
FRONT_OBSTACLE_STOP_M: float = float(os.getenv("SENTRA_FRONT_STOP_M", "0.40"))
REAR_OBSTACLE_STOP_M: float = float(os.getenv("SENTRA_REAR_STOP_M", "0.35"))
CLIFF_STOP: bool = True

# ── Motor limits ──────────────────────────────────────────────────────────────
MAX_SPEED_MULTIPLIER: float = 1.0     # hard ceiling for |left|/|right| duty factor
DEFAULT_SPEED_MULTIPLIER: float = float(os.getenv("SENTRA_SPEED_MULT", "0.5"))

# ── Command timeout (watchdog) ────────────────────────────────────────────────
# Manual streaming commands must repeat at least this often or motors stop.
MANUAL_CMD_TIMEOUT_S: float = float(os.getenv("SENTRA_CMD_TIMEOUT_S", "0.6"))
# Autonomous modes get a looser timeout (their loops re-issue periodically).
AUTONOMOUS_CMD_TIMEOUT_S: float = float(os.getenv("SENTRA_AUTO_CMD_TIMEOUT_S", "2.0"))

# ── Motion smoothing (Phase 2) ────────────────────────────────────────
# Ramp rates in percent duty per second. 150 %/s → 0→50 % in ~0.33 s.
ACCEL_PCT_PER_S: float = float(os.getenv("SENTRA_ACCEL_PCT_PER_S", "150"))
DECEL_PCT_PER_S: float = float(os.getenv("SENTRA_DECEL_PCT_PER_S", "300"))
MOTION_TICK_S: float = 0.05            # 20 Hz ramp loop
BRAKE_HOLD_S: float = float(os.getenv("SENTRA_BRAKE_HOLD_S", "0.5"))  # active brake then release
MAX_MOVE_DURATION_S: float = 30.0      # cap for REST one-shot moves

# ── Patrol loop (Phase 7 will replace with routes; kept for compatibility) ──
PATROL_OBSTACLE_M: float = float(os.getenv("SENTRA_PATROL_OBSTACLE_M", "1.0"))
PATROL_TICK_S: float = 0.5

# ── Patrol engine (Phase 7) ──────────────────────────────────────────────
# Duty (%) applied while cruising between waypoints.
PATROL_CRUISE_DUTY: float = float(os.getenv("SENTRA_PATROL_CRUISE_DUTY", "40"))
# Give up a waypoint after this long without confirming it.
PATROL_WAYPOINT_TIMEOUT_S: float = float(os.getenv("SENTRA_PATROL_WP_TIMEOUT_S", "60"))
# While blocked: re-check the path this often.
PATROL_BLOCK_POLL_S: float = 0.5
# If blocked longer than this, abort the patrol (waypoint timeout governs).
PATROL_MAX_BLOCK_S: float = 30.0
# A tag confirms a waypoint if seen within this freshness window.
PATROL_CONFIRM_FRESH_S: float = 3.0
# Path storage
PATROL_ROUTES_PATH: str = os.path.expanduser(
    os.getenv("SENTRA_PATROL_ROUTES_PATH", "~/sentra_data/patrol_routes.json"))

# ── Return-to-dock (Phase 8) ──────────────────────────────────────────────
DOCK_SEARCH_TURN_DUTY: float = float(os.getenv("SENTRA_DOCK_SEARCH_DUTY", "30"))
DOCK_SEARCH_TIMEOUT_S: float = float(os.getenv("SENTRA_DOCK_SEARCH_TIMEOUT_S", "25"))
DOCK_APPROACH_DUTY: float = float(os.getenv("SENTRA_DOCK_APPROACH_DUTY", "35"))
DOCK_SLOW_M: float = float(os.getenv("SENTRA_DOCK_SLOW_M", "0.8"))   # slow down inside this
DOCK_NEAR_DUTY: float = float(os.getenv("SENTRA_DOCK_NEAR_DUTY", "20"))
DOCK_STOP_M: float = float(os.getenv("SENTRA_DOCK_STOP_M", "0.30"))  # reached the dock
DOCK_ALIGN_TOL_DEG: float = float(os.getenv("SENTRA_DOCK_ALIGN_TOL_DEG", "8"))
DOCK_ALIGN_DUTY: float = float(os.getenv("SENTRA_DOCK_ALIGN_DUTY", "18"))
DOCK_APPROACH_TIMEOUT_S: float = float(os.getenv("SENTRA_DOCK_APPROACH_TIMEOUT_S", "60"))
DOCK_STEER_GAIN: float = float(os.getenv("SENTRA_DOCK_STEER_GAIN", "0.45"))  # duty per bearing°
DOCK_TAG_LOST_GRACE_S: float = float(os.getenv("SENTRA_DOCK_TAG_LOST_GRACE_S", "1.5"))

# ── Navigation / voice (Phase 9) ─────────────────────────────────────
# Generic go-to navigation reuses the docking approach dynamics.
NAV_SEARCH_TIMEOUT_S: float = float(os.getenv("SENTRA_NAV_SEARCH_TIMEOUT_S", "30"))
NAV_APPROACH_TIMEOUT_S: float = float(os.getenv("SENTRA_NAV_APPROACH_TIMEOUT_S", "90"))
NAV_ARRIVE_M: float = float(os.getenv("SENTRA_NAV_ARRIVE_M", "0.45"))  # stop this far from a location tag

# ── Camera (vision architecture decision) ─────────────────────────────────────
# The mounted mobile phone is the ONLY vision source. There is no Pi camera and
# no USB webcam. Frames reach the backend via the call-service WS/HTTP path and
# (from the vision phase onward) via a dedicated vision ingress.
VISION_SOURCE: str = "mobile_phone_camera"
