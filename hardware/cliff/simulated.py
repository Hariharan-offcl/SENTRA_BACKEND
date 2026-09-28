import logging
from .base import BaseCliffSensor

logger = logging.getLogger(__name__)

class SimulatedCliffSensor(BaseCliffSensor):
    """Mock cliff sensor implementation."""
    def __init__(self):
        self.initialized = False

    def initialize(self):
        self.initialized = True
        logger.info("SimulatedCliffSensor: Initialized")

    def shutdown(self):
        self.initialized = False
        logger.info("SimulatedCliffSensor: Shutdown")

    def read(self) -> dict:
        return {"left": False, "right": False}

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "simulated"}
