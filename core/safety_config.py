"""
SENTRA — Runtime-configurable safety thresholds (Phase 3).

Phase 1/2 read static values from core.config. Phase 3 makes thresholds
mutable at runtime (settings API / Flutter app), with hard bounds so the app
can never configure the rover into an unsafe state.

Startup values come from the same SENTRA_* env vars as before, so existing
deployments behave identically after upgrade.

NOT persisted yet — resets to env defaults on backend restart.
Persistence (SQLite/JSON) lands with the settings/persistence phase.
"""

from __future__ import annotations

import logging
import threading

from core import config as core_config

logger = logging.getLogger(__name__)

# Hard bounds — the runtime API cannot set values outside these.
# Safety margins are floor-limited: you cannot set a stop distance so small
# that the rover cannot physically stop in time.
LIMITS: dict[str, tuple[float, float]] = {
    "front_obstacle_stop_m": (0.10, 2.00),
    "rear_obstacle_stop_m": (0.10, 2.00),
    "patrol_obstacle_m": (0.20, 2.00),
    "manual_cmd_timeout_s": (0.20, 5.00),
    "autonomous_cmd_timeout_s": (0.50, 10.00),
    "accel_pct_per_s": (20.0, 500.0),
    "decel_pct_per_s": (50.0, 1000.0),
    "max_speed_multiplier": (0.10, 1.00),
}


class SafetyConfig:
    """Thread-safe runtime safety configuration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, float] = {
            "front_obstacle_stop_m": core_config.FRONT_OBSTACLE_STOP_M,
            "rear_obstacle_stop_m": core_config.REAR_OBSTACLE_STOP_M,
            "cliff_stop": 1.0 if core_config.CLIFF_STOP else 0.0,
            "patrol_obstacle_m": core_config.PATROL_OBSTACLE_M,
            "manual_cmd_timeout_s": core_config.MANUAL_CMD_TIMEOUT_S,
            "autonomous_cmd_timeout_s": core_config.AUTONOMOUS_CMD_TIMEOUT_S,
            "accel_pct_per_s": core_config.ACCEL_PCT_PER_S,
            "decel_pct_per_s": core_config.DECEL_PCT_PER_S,
            "max_speed_multiplier": core_config.MAX_SPEED_MULTIPLIER,
        }

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, key: str) -> float:
        with self._lock:
            return self._values[key]

    def get_bool(self, key: str) -> bool:
        return self.get(key) > 0.5

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._values)

    def limits(self) -> dict[str, list[float]]:
        return {k: list(v) for k, v in LIMITS.items()}

    # ── Writes ────────────────────────────────────────────────────────────────

    def update(self, updates: dict) -> dict:
        """Validate + apply atomically: if ANY key is invalid, NOTHING changes.
        Returns {'updated': [...], 'rejected': {key: reason}}."""
        rejected: dict[str, str] = {}
        validated: dict[str, float] = {}

        # Pass 1 — validate every key first.
        for key, value in updates.items():
            if key not in self._values:
                rejected[key] = "unknown_key"
                continue
            if key == "cliff_stop":
                validated[key] = 1.0 if bool(value) else 0.0
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                rejected[key] = "not_a_number"
                continue
            lo, hi = LIMITS.get(key, (float("-inf"), float("inf")))
            if not (lo <= v <= hi):
                rejected[key] = f"out_of_bounds [{lo}, {hi}]"
                continue
            validated[key] = v

        # Pass 2 — all-or-nothing: apply only if the whole batch validated.
        if rejected:
            for key, reason in rejected.items():
                logger.warning("Safety config rejected %s: %s", key, reason)
            return {"updated": [], "rejected": rejected}

        with self._lock:
            self._values.update(validated)
            updated = list(validated.keys())

        if updated:
            logger.info("Safety config updated: %s", {k: self._values[k] for k in updated})
        return {"updated": updated, "rejected": rejected}

    def reset(self) -> dict:
        """Restore env-var defaults."""
        with self._lock:
            self.__init__()  # re-reads core_config defaults
        logger.info("Safety config reset to defaults")
        return self.snapshot()


# ── Process-wide singleton ────────────────────────────────────────────────────

_safety_config = SafetyConfig()


def get_safety_config() -> SafetyConfig:
    return _safety_config
