import logging
from .base import BaseEncoder

logger = logging.getLogger(__name__)

class SimulatedEncoder(BaseEncoder):
    """Mock encoder implementation."""
    def __init__(self):
        self.initialized = False
        self.left_dist = 0.0
        self.right_dist = 0.0

    def initialize(self):
        self.initialized = True
        logger.info("SimulatedEncoder: Initialized")

    def shutdown(self):
        self.initialized = False
        logger.info("SimulatedEncoder: Shutdown")

    def get_distance(self) -> dict:
        return {"left": self.left_dist, "right": self.right_dist}

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "simulated"}
