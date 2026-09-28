import logging
from .base import BaseMotor

logger = logging.getLogger(__name__)

class SimulatedMotor(BaseMotor):
    """Mock motor implementation for simulation mode."""
    def __init__(self):
        self.left_speed = 0.0
        self.right_speed = 0.0
        self.initialized = False

    def initialize(self):
        self.initialized = True
        logger.info("SimulatedMotor: Initialized")

    def shutdown(self):
        self.left_speed = 0.0
        self.right_speed = 0.0
        self.initialized = False
        logger.info("SimulatedMotor: Shutdown")

    def set_speed(self, left: float, right: float):
        self.left_speed = left
        self.right_speed = right
        # In a real sim, this would update a physics model
        # logger.debug(f"SimMotor speed: L={left:.2f} R={right:.2f}")

    def status(self) -> dict:
        return {
            "left_speed": self.left_speed,
            "right_speed": self.right_speed,
            "initialized": self.initialized,
            "mode": "simulated"
        }
