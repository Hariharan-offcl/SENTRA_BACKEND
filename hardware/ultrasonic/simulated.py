import logging
from .base import BaseUltrasonic

logger = logging.getLogger(__name__)

class SimulatedUltrasonic(BaseUltrasonic):
    """Mock ultrasonic sensor for simulation mode."""
    def __init__(self, name="sensor"):
        self.name = name
        self.distance = 2.0 # Default 2 meters
        self.initialized = False

    def initialize(self):
        self.initialized = True
        logger.info(f"SimulatedUltrasonic ({self.name}): Initialized")

    def shutdown(self):
        self.initialized = False
        logger.info(f"SimulatedUltrasonic ({self.name}): Shutdown")

    def read_distance(self) -> float:
        return self.distance

    def status(self) -> dict:
        return {
            "name": self.name,
            "distance": self.distance,
            "initialized": self.initialized,
            "mode": "simulated"
        }
