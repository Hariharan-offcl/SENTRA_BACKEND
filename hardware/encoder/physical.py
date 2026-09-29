import logging

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

from .base import BaseEncoder
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)

# Phase 2: GPIO 23 is IN4 on the L298N — never an encoder default. These
# defaults match services/encoder_service.py (which the Phase 0 audit
# confirmed refuses pin 23; the HAL copy now refuses it too).
MOTOR_PINS = {12, 27, 17, 13, 22, 23}
ULTRASONIC_PINS = {24, 25, 5, 6}
CLIFF_DEFAULT_PINS = {19, 26}
_RESERVED = MOTOR_PINS | ULTRASONIC_PINS | CLIFF_DEFAULT_PINS


class PhysicalEncoder(BaseEncoder):
    """Real wheel encoder implementation."""

    def __init__(self, left_pin=20, right_pin=16):
        self.left_pin = left_pin
        self.right_pin = right_pin
        self.initialized = False
        self.left_count = 0
        self.right_count = 0
        self._callbacks = []

    def initialize(self):
        try:
            if not _LGPIO_AVAILABLE:
                raise RuntimeError("lgpio not available (dev machine?)")
            if self.left_pin in _RESERVED or self.right_pin in _RESERVED:
                raise RuntimeError(
                    f"encoder pins {self.left_pin}/{self.right_pin} collide with "
                    f"reserved pins {sorted(_RESERVED)} — set real encoder pins")
            h = gpio_manager.chip
            if h is None:
                raise RuntimeError("GPIO chip not available")

            if not gpio_manager.claim_input(self.left_pin, owner="encoder.hal"):
                raise RuntimeError(f"pin {self.left_pin} unavailable")
            if not gpio_manager.claim_input(self.right_pin, owner="encoder.hal"):
                raise RuntimeError(f"pin {self.right_pin} unavailable")

            self._callbacks = [
                lgpio.callback(h, self.left_pin, lgpio.BOTH_EDGES, self._on_left_tick),
                lgpio.callback(h, self.right_pin, lgpio.BOTH_EDGES, self._on_right_tick),
            ]

            self.initialized = True
            logger.info("PhysicalEncoder: Initialized Left=%d Right=%d (shared handle)",
                        self.left_pin, self.right_pin)
        except Exception as e:
            logger.error("PhysicalEncoder: Initialization failed: %s", e)
            raise

    def _on_left_tick(self, chip, gpio, level, tick):
        self.left_count += 1

    def _on_right_tick(self, chip, gpio, level, tick):
        self.right_count += 1

    def shutdown(self):
        for cb in self._callbacks:
            try:
                cb.cancel()
            except Exception:
                pass
        self._callbacks = []
        self.initialized = False
        logger.info("PhysicalEncoder: Shutdown")

    def get_distance(self) -> dict:
        # Placeholder: convert ticks to meters based on wheel circumference
        TICK_TO_METER = 0.001
        return {
            "left": self.left_count * TICK_TO_METER,
            "right": self.right_count * TICK_TO_METER,
        }

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "physical"}
