import logging
from .base import BaseCurrentSensor

logger = logging.getLogger(__name__)

class SimulatedCurrentSensor(BaseCurrentSensor):
    """Mock current sensor implementation."""
    def __init__(self):
        self.initialized = False

    def initialize(self):
        self.initialized = True
        logger.info("SimulatedCurrentSensor: Initialized")

    def shutdown(self):
        self.initialized = False
        logger.info("SimulatedCurrentSensor: Shutdown")

    def read(self) -> dict:
        return {
            "voltage": 12.4,
            "current": 0.15,
            "power": 1.86
        }

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "simulated"}
