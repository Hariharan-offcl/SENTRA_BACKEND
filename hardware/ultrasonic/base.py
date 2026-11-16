from abc import ABC, abstractmethod

class BaseUltrasonic(ABC):
    """Interface for ultrasonic distance sensors."""

    @abstractmethod
    def initialize(self): pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def read_distance(self) -> float:
        """Return distance in meters"""
        pass

    @abstractmethod
    def status(self) -> dict: pass
