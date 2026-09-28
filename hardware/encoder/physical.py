import logging
import lgpio
from .base import BaseEncoder
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)

class PhysicalEncoder(BaseEncoder):
    """Real wheel encoder implementation."""

    def __init__(self, left_pin=23, right_pin=16):
        self.left_pin = left_pin
        self.right_pin = right_pin
        self.initialized = False
        self.left_count = 0
        self.right_count = 0

    def initialize(self):
        try:
            h = gpio_manager.chip
            if h is None:
                raise RuntimeError("GPIO chip not available")

            lgpio.gpio_claim_input(h, self.left_pin)
            lgpio.gpio_claim_input(h, self.right_pin)

            # Setup callbacks
            lgpio.gpio_callback(h, self.left_pin, lgpio.BOTH_EDGES, self._on_left_tick)
            lgpio.gpio_callback(h, self.right_pin, lgpio.BOTH_EDGES, self._on_right_tick)

            self.initialized = True
            logger.info(f"PhysicalEncoder: Initialized Left={self.left_pin} Right={self.right_pin}")
        except Exception as e:
            logger.error(f"PhysicalEncoder: Initialization failed: {e}")
            raise

    def _on_left_tick(self, chip, gpio, level, tick):
        self.left_count += 1

    def _on_right_tick(self, chip, gpio, level, tick):
        self.right_count += 1

    def shutdown(self):
        self.initialized = False
        logger.info("PhysicalEncoder: Shutdown")

    def get_distance(self) -> dict:
        # Placeholder: convert ticks to meters based on wheel circumference
        TICK_TO_METER = 0.001
        return {
            "left": self.left_count * TICK_TO_METER,
            "right": self.right_count * TICK_TO_METER
        }

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "physical"}
