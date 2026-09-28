from abc import ABC, abstractmethod

class BaseCurrentSensor(ABC):
    """Interface for current and voltage sensors."""

    @abstractmethod
    def initialize(self): pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def read(self) -> dict:
        """Return voltage and current"""
        pass

    @abstractmethod
    def status(self) -> dict: pass
