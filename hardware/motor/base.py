from abc import ABC, abstractmethod

class BaseMotor(ABC):
    """Interface for motor control."""

    @abstractmethod
    def initialize(self):
        """Setup GPIO pins and PWM"""
        pass

    @abstractmethod
    def shutdown(self):
        """Stop all motors and cleanup GPIO"""
        pass

    @abstractmethod
    def set_speed(self, left: float, right: float):
        """
        Set wheel speeds.
        :param left: Range -1.0 (full reverse) to 1.0 (full forward)
        :param right: Range -1.0 (full reverse) to 1.0 (full forward)
        """
        pass

    @abstractmethod
    def status(self) -> dict:
        """Return current motor state and targets"""
        pass
