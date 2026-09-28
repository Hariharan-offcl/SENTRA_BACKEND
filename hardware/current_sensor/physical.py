import logging
import time
import smbus2
from typing import Optional
from .base import BaseCurrentSensor
from hardware.bus_manager import bus_manager

logger = logging.getLogger(__name__)

class PhysicalCurrentSensor(BaseCurrentSensor):
    """
    Implementation for INA219 I2C Current/Voltage Sensor.
    The INA219 measures shunt voltage and bus voltage to calculate power and current.
    """

    def __init__(self, address=0x40):
        self.address = address
        self.initialized = False

        # INA219 Constants
        self._REG_CONFIG = 0x00
        self._REG_SHUNTVOLT = 0x01
        self._REG_BUSVOLT = 0x02
        self._REG_POWER = 0x03
        self._REG_CURRENT = 0x04

        # Calibration factor: For typical 0.1 Ohm shunt,
        # current = shunt_voltage / 0.1.
        # INA219 LSB for current is typically 100uA (0.1mA) depending on config.
        self.current_lsb = 0.1  # mA per LSB

    def initialize(self):
        try:
            bus = bus_manager.bus
            if bus is None:
                raise RuntimeError("I2C bus not available")

            # Configure INA219:
            # Range 16V, Gain 1 (40mV), 12-bit ADC
            # Binary: 0x3967 or similar based on datasheet
            # Simplified: we just check if it responds.
            bus.read_byte_data(self.address, self._REG_CONFIG)

            self.initialized = True
            logger.info(f"PhysicalCurrentSensor: Initialized INA219 at {hex(self.address)}")
        except Exception as e:
            logger.error(f"PhysicalCurrentSensor: Initialization failed: {e}")
            self.initialized = False

    def shutdown(self):
        self.initialized = False
        logger.info("PhysicalCurrentSensor: Shutdown")

    def _read_word(self, reg: int) -> int:
        bus = bus_manager.bus
        if bus is None: return 0
        # read_word_data returns 16-bit value
        return bus.read_word_data(self.address, reg)

    def read(self) -> dict:
        if not self.initialized:
            return {"voltage": 0.0, "current": 0.0, "power": 0.0}

        try:
            # Bus Voltage: 16mV per LSB
            bus_raw = self._read_word(self._REG_BUSVOLT)
            voltage = (bus_raw & 0x3FFF) * 0.004

            # Current: Based on LSB config
            curr_raw = self._read_word(self._REG_CURRENT)
            # Current can be negative (bidirectional)
            if curr_raw > 32767:
                curr_raw -= 65536
            current = curr_raw * self.current_lsb

            # Power
            pwr_raw = self._read_word(self._REG_POWER)
            power = pwr_raw * 0.001 # Dummy scale for power

            return {
                "voltage": round(voltage, 3),
                "current": round(current, 2), # mA
                "power": round(power, 3)
            }
        except Exception as e:
            logger.error(f"PhysicalCurrentSensor read error: {e}")
            return {"voltage": 0.0, "current": 0.0, "power": 0.0}

    def status(self) -> dict:
        return {"initialized": self.initialized, "mode": "physical"}
