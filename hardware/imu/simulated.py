import logging
from .base import BaseIMU

logger = logging.getLogger(__name__)

class SimulatedIMU(BaseIMU):
    """Mock IMU implementation."""
    def __init__(self):
        self.initialized = False

    def initialize(self):
        self.initialized = True
        logger.info("SimulatedIMU: Initialized")

    def shutdown(self):
        self.initialized = False
        logger.info("SimulatedIMU: Shutdown")

    def get_data(self) -> dict:
        return {
            "accel": {"x": 0.0, "y": 0.0, "z": 9.81},
            "gyro": {"x": 0.0, "y": 0.0, "z": 0.0},
            "heading": 0.0,
            "pitch": 0.0,
            "roll": 0.0
        }

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "simulated"}
