import logging
import os

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

from .base import BaseCliffSensor
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)

# Defaults mirror services/cliff_service.py. NOTE (Phase 0 audit): the physical
# cliff GPIO pins are NOT yet confirmed on hardware — these are placeholders.
# Override via SENTRA_CLIFF_LEFT_GPIO / SENTRA_CLIFF_RIGHT_GPIO once wired.
CLIFF_ENABLED = os.getenv("SENTRA_CLIFF_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
CLIFF_LEFT_GPIO = int(os.getenv("SENTRA_CLIFF_LEFT_GPIO", "4"))
CLIFF_RIGHT_GPIO = int(os.getenv("SENTRA_CLIFF_RIGHT_GPIO", "8"))
CLIFF_ACTIVE_HIGH = os.getenv("SENTRA_CLIFF_ACTIVE_HIGH", "true").strip().lower() in (
    "1", "true", "yes", "on")


class PhysicalCliffSensor(BaseCliffSensor):
    """Digital IR cliff sensors (TCRT5000-style comparator boards).

    The active level is configurable: the module drives its pin HIGH (or LOW)
    when an edge / drop-off is detected. Until the pins are confirmed on the
    real build, initialization failure keeps this sensor in a clean simulated
    state (reads no cliff) instead of crashing the HAL boot.
    """

    def __init__(self, left_pin: int | None = None, right_pin: int | None = None):
        self.left_pin = int(left_pin if left_pin is not None else CLIFF_LEFT_GPIO)
        self.right_pin = int(right_pin if right_pin is not None else CLIFF_RIGHT_GPIO)
        self.initialized = False
        self._simulated = True  # False only after a successful GPIO claim

    def initialize(self):
        if not CLIFF_ENABLED or not _LGPIO_AVAILABLE:
            logger.info("PhysicalCliffSensor: disabled or lgpio unavailable — simulated")
            return
        try:
            if not gpio_manager.claim_input(self.left_pin, owner="cliff.hal"):
                raise RuntimeError(f"pin {self.left_pin} unavailable")
            if not gpio_manager.claim_input(self.right_pin, owner="cliff.hal"):
                raise RuntimeError(f"pin {self.right_pin} unavailable")
            self.initialized = True
            self._simulated = False
            logger.info("PhysicalCliffSensor: initialized on GPIO %d/%d (active-%s)",
                        self.left_pin, self.right_pin,
                        "high" if CLIFF_ACTIVE_HIGH else "low")
        except Exception as e:
            logger.warning("PhysicalCliffSensor: init failed — simulated (%s)", e)
            self.initialized = False

    def shutdown(self):
        self.initialized = False
        logger.info("PhysicalCliffSensor: Shutdown")

    def read(self) -> dict:
        if not self.initialized:
            return {"left": False, "right": False}
        try:
            h = gpio_manager.chip
            if h is None:
                return {"left": False, "right": False}
            left = bool(lgpio.gpio_read(h, self.left_pin))
            right = bool(lgpio.gpio_read(h, self.right_pin))
            if not CLIFF_ACTIVE_HIGH:
                left, right = not left, not right
            return {"left": left, "right": right}
        except Exception as e:
            logger.error("PhysicalCliffSensor read error: %s", e)
            return {"left": False, "right": False}

    def status(self) -> dict:
        return {
            "initialized": self.initialized,
            "mode": "simulated" if self._simulated else "physical",
            "left_gpio": self.left_pin,
            "right_gpio": self.right_pin,
            "active_high": CLIFF_ACTIVE_HIGH,
        }
