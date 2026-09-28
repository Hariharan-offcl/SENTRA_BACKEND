import logging
import lgpio
from .base import BaseMotor
from hardware.gpio_manager import gpio_manager

logger = logging.getLogger(__name__)

class PhysicalMotor(BaseMotor):
    """Real L298N driver implementation for Raspberry Pi 5."""

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
            h = gpio_manager.chip
            if h is None:
                raise RuntimeError("GPIO chip not available")

            # Set all as outputs
            for pin in [self.ENA, self.IN1, self.IN2, self.ENB, self.IN3, self.IN4]:
                lgpio.gpio_claim_output(h, pin)

            # Initialize to stop
            self.set_speed(0, 0)
            self.initialized = True
            logger.info("PhysicalMotor: Initialized L298N driver")
        except Exception as e:
            logger.error(f"PhysicalMotor: Initialization failed: {e}")
            raise

    def shutdown(self):
        self.set_speed(0, 0)
        self.initialized = False
        logger.info("PhysicalMotor: Shutdown")

    def set_speed(self, left: float, right: float):
        """
        L298N logic:
        Forward: IN1=H, IN2=L, ENA=PWM
        Reverse: IN1=L, IN2=H, ENA=PWM
        """
        h = gpio_manager.chip
        if h is None: return

        # Left Motor
        if left > 0:
            lgpio.gpio_write(h, self.IN1, 1)
            lgpio.gpio_write(h, self.IN2, 0)
            duty = int(left * 1000000) # scaled to micro-seconds or percentage depending on lgpio version
            # Using tx_pwm for Pi 5
            lgpio.tx_pwm(h, self.ENA, 1000, int(left * 100))
        elif left < 0:
            lgpio.gpio_write(h, self.IN1, 0)
            lgpio.gpio_write(h, self.IN2, 1)
            lgpio.tx_pwm(h, self.ENA, 1000, int(abs(left) * 100))
        else:
            lgpio.gpio_write(h, self.IN1, 0)
            lgpio.gpio_write(h, self.IN2, 0)
            lgpio.tx_pwm(h, self.ENA, 1000, 0)

        # Right Motor
        if right > 0:
            lgpio.gpio_write(h, self.IN3, 1)
            lgpio.gpio_write(h, self.IN4, 0)
            lgpio.tx_pwm(h, self.ENB, 1000, int(right * 100))
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

    def status(self) -> dict:
        return {
            "left_speed": self._left_target,
            "right_speed": self._right_target,
            "initialized": self.initialized,
            "mode": "physical"
        }
