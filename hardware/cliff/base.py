from abc import ABC, abstractmethod

class BaseCliffSensor(ABC):
    """Interface for IR cliff sensors."""

    @abstractmethod
    def initialize(self): pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def read(self) -> dict:
        """Return { 'left': bool, 'right': bool }"""
        pass

    @abstractmethod
    def status(self) -> dict: pass
