from abc import ABC, abstractmethod

class BaseEncoder(ABC):
    """Interface for wheel encoders."""

    @abstractmethod
    def initialize(self): pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def get_distance(self) -> dict:
        """Return { 'left': float, 'right': float } in meters"""
        pass

    @abstractmethod
    def status(self) -> dict: pass
