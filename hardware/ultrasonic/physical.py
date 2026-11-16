import logging
import time

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

from .base import BaseUltrasonic
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)

class PhysicalUltrasonic(BaseUltrasonic):
    """Real HC-SR04 implementation for Raspberry Pi 5."""

    def __init__(self, name: str, trig_pin: int, echo_pin: int):
        self.name = name
        self.trig = trig_pin
        self.echo = echo_pin
        self.initialized = False

    def initialize(self):
        try:
            if not _LGPIO_AVAILABLE:
                raise RuntimeError("lgpio not available (dev machine?)")
            if not gpio_manager.claim_output(self.trig, owner="ultrasonic.hal"):
                raise RuntimeError(f"pin {self.trig} unavailable")
            if not gpio_manager.claim_input(self.echo, owner="ultrasonic.hal"):
                raise RuntimeError(f"pin {self.echo} unavailable")

            h = gpio_manager.chip
            if h is not None:
                lgpio.gpio_write(h, self.trig, 0)
            self.initialized = True
            logger.info(f"PhysicalUltrasonic ({self.name}): Initialized Trig={self.trig} Echo={self.echo}")
        except Exception as e:
            logger.error(f"PhysicalUltrasonic ({self.name}): Initialization failed: {e}")
            raise

    def shutdown(self):
        self.initialized = False
        logger.info(f"PhysicalUltrasonic ({self.name}): Shutdown")

    def read_distance(self) -> float:
        h = gpio_manager.chip
        if h is None or not self.initialized:
            return 4.0 # Safe default

        try:
            # Trigger pulse
            lgpio.gpio_write(h, self.trig, 1)
            time.sleep(0.00001) # 10us
            lgpio.gpio_write(h, self.trig, 0)

            # Measure echo
            start = time.monotonic()
            # Busy wait for HIGH (with timeout)
            timeout = start + 0.05
            while lgpio.gpio_read(h, self.echo) == 0:
                if time.monotonic() > timeout:
                    return 4.0

            t_start = time.monotonic()
            while lgpio.gpio_read(h, self.echo) == 1:
                if time.monotonic() > timeout:
                    break
            t_end = time.monotonic()

            duration = t_end - t_start
            # Speed of sound = 343 m/s. Distance = (time * speed) / 2
            distance = (duration * 343.0) / 2.0
            return round(distance, 3)
        except Exception as e:
            logger.error(f"PhysicalUltrasonic ({self.name}) read error: {e}")
            return 4.0

    def status(self) -> dict:
        return {
            "name": self.name,
            "distance": self.read_distance(),
            "initialized": self.initialized,
            "mode": "physical"
        }
