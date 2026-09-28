from typing import Dict, Optional
import logging
import lgpio

logger = logging.getLogger("sentra.hardware.gpio")

class GPIOManager:
    """
    Centralized manager for the lgpio chip handle.
    Prevents multiple processes/services from opening the chip simultaneously.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            try:
                # Open the first gpiochip (standard for Pi 5)
                cls._instance.chip = lgpio.gpiochip_open(0)
                logger.info("GPIOManager: Successfully opened gpiochip 0")
            except Exception as e:
                logger.error(f"GPIOManager: Failed to open gpiochip: {e}")
                cls._instance.chip = None
        return cls._instance

    def close(self):
        if self.chip:
            lgpio.gpiochip_close(self.chip)
            self.chip = None
            logger.info("GPIOManager: gpiochip closed")

# Singleton instance
gpio_manager = GPIOManager()
