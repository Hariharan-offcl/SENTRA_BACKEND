import logging

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

from .base import BaseMotor
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)


class PhysicalMotor(BaseMotor):
    """Real L298N driver implementation for Raspberry Pi 5.

    Phase 2: every claim and every write goes through the shared
    gpio_manager handle — the same handle the brake uses, so drive and
    brake can never fight through different chip consumers.
    """

    def __init__(self):
        # Configuration (matching original spec)
        self.ENA = 12
        self.IN1 = 27
        self.IN2 = 17
        self.ENB = 13
        self.IN3 = 22
        self.IN4 = 23
        self.initialized = False
        self._left_target = 0.0
        self._right_target = 0.0

    def initialize(self):
        try:
            if not _LGPIO_AVAILABLE:
                raise RuntimeError("lgpio not available (dev machine?)")
            h = gpio_manager.chip
            if h is None:
                raise RuntimeError("GPIO chip not available")

            for pin in (self.ENA, self.IN1, self.IN2, self.ENB, self.IN3, self.IN4):
                if not gpio_manager.claim_output(pin, owner="motor", initial=0):
                    raise RuntimeError(f"pin {pin} unavailable (already claimed)")

            self.set_speed(0, 0)
            self.initialized = True
            logger.info("PhysicalMotor: Initialized L298N driver on shared chip handle")
        except Exception as e:
            logger.error(f"PhysicalMotor: Initialization failed: {e}")
            raise

    def shutdown(self):
        try:
            self.set_speed(0, 0)
        except Exception:
            pass
        self.initialized = False
        logger.info("PhysicalMotor: Shutdown")

    def set_speed(self, left: float, right: float):
        """L298N logic: forward IN1=H/IN2=L, reverse IN1=L/IN2=H, PWM on ENx.
        left/right in -1.0..1.0 (fraction of full duty)."""
        h = gpio_manager.chip
        if h is None:
            return

        # Left motor
        if left > 0:
            lgpio.gpio_write(h, self.IN1, 1)
            lgpio.gpio_write(h, self.IN2, 0)
            lgpio.tx_pwm(h, self.ENA, 1000, int(abs(left) * 100))
        elif left < 0:
            lgpio.gpio_write(h, self.IN1, 0)
            lgpio.gpio_write(h, self.IN2, 1)
            lgpio.tx_pwm(h, self.ENA, 1000, int(abs(left) * 100))
        else:
            lgpio.gpio_write(h, self.IN1, 0)
            lgpio.gpio_write(h, self.IN2, 0)
            lgpio.tx_pwm(h, self.ENA, 1000, 0)

        # Right motor
        if right > 0:
            lgpio.gpio_write(h, self.IN3, 1)
            lgpio.gpio_write(h, self.IN4, 0)
            lgpio.tx_pwm(h, self.ENB, 1000, int(abs(right) * 100))
        elif right < 0:
            lgpio.gpio_write(h, self.IN3, 0)
            lgpio.gpio_write(h, self.IN4, 1)
            lgpio.tx_pwm(h, self.ENB, 1000, int(abs(right) * 100))
        else:
            lgpio.gpio_write(h, self.IN3, 0)
            lgpio.gpio_write(h, self.IN4, 0)
            lgpio.tx_pwm(h, self.ENB, 1000, 0)

        self._left_target = left
        self._right_target = right

    def brake(self, hold_s: float = 0.5) -> None:
        """Active brake: INx both HIGH + full duty = shorted motor terminals
        on the L298N (dynamic braking). Same handle/pins as set_speed.
        Note: holds synchronously — callers are background threads."""
        import time
        h = gpio_manager.chip
        if h is None:
            self.set_speed(0, 0)
            return
        # Stop PWM drive first
        lgpio.tx_pwm(h, self.ENA, 1000, 0)
        lgpio.tx_pwm(h, self.ENB, 1000, 0)
        # Short both terminals
        for a, b in ((self.IN1, self.IN2), (self.IN3, self.IN4)):
            lgpio.gpio_write(h, a, 1)
            lgpio.gpio_write(h, b, 1)
        lgpio.tx_pwm(h, self.ENA, 1000, 100)
        lgpio.tx_pwm(h, self.ENB, 1000, 100)
        time.sleep(hold_s)
        # Release to neutral
        for en, a, b in ((self.ENA, self.IN1, self.IN2),
                         (self.ENB, self.IN3, self.IN4)):
            lgpio.gpio_write(h, a, 0)
            lgpio.gpio_write(h, b, 0)
            lgpio.tx_pwm(h, en, 1000, 0)
        self._left_target = 0.0
        self._right_target = 0.0

    def status(self) -> dict:
        return {
            "left_speed": self._left_target,
            "right_speed": self._right_target,
            "initialized": self.initialized,
            "mode": "physical",
        }
