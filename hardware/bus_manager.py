from typing import Optional
import logging

try:
    import smbus2
    _SMBUS_AVAILABLE = True
except ImportError:
    _SMBUS_AVAILABLE = False

logger = logging.getLogger("sentra.hardware.bus")

class BusManager:
    """
    Centralized manager for I2C bus handles.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            if not _SMBUS_AVAILABLE:
                logger.warning("BusManager: smbus2 not available (dev machine?) — bus handle stays None")
                cls._instance.bus = None
                return cls._instance
            try:
                # I2C Bus 1 is standard on Raspberry Pi
                cls._instance.bus = smbus2.SMBus(1)
                logger.info("BusManager: Successfully opened I2C Bus 1")
            except Exception as e:
                logger.error(f"BusManager: Failed to open I2C Bus 1: {e}")
                cls._instance.bus = None
        return cls._instance

    def close(self):
        if self.bus:
            self.bus.close()
            self.bus = None
            logger.info("BusManager: I2C Bus 1 closed")

# Singleton instance
bus_manager = BusManager()
