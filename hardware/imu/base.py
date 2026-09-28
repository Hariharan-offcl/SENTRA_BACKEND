from abc import ABC, abstractmethod

class BaseIMU(ABC):
    """Interface for Inertial Measurement Unit."""

    @abstractmethod
    def initialize(self): pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def get_data(self) -> dict:
        """Return accel, gyro, and heading"""
        pass

    @abstractmethod
    def status(self) -> dict: pass
