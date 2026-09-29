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

    def brake(self, hold_s: float = 0.5) -> None:
        """Active brake (both L298N terminals shorted), then release to
        neutral. Default: coast to a stop — physical drivers override with
        the real short-brake sequence on the SAME pins/handle as set_speed
        (Phase 2: one chip, one path, no split-brain)."""
        self.set_speed(0, 0)

    @abstractmethod
    def status(self) -> dict:
        """Return current motor state and targets"""
        pass
