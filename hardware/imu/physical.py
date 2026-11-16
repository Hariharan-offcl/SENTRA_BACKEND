import logging
import time
import json
import os
from typing import Optional

try:
    import smbus2
    _SMBUS_AVAILABLE = True
except ImportError:
    _SMBUS_AVAILABLE = False

from .base import BaseIMU
from hardware.bus_manager import bus_manager

logger = logging.getLogger(__name__)

CALIB_FILE = os.path.expanduser("~/sentra_data/imu_calib.json")

class PhysicalIMU(BaseIMU):
    """MPU6050 implementation via I2C with calibration and bias compensation."""

    def __init__(self, address=0x68):
        self.address = address
        self.initialized = False
        self.yaw = 0.0
        self.last_time = 0.0

        # Calibration offsets
        self.offsets = {"gx": 0.0, "gy": 0.0, "gz": 0.0}
        self._is_calibrated = False

    def initialize(self):
        try:
            if not _SMBUS_AVAILABLE:
                raise RuntimeError("smbus2 not available (dev machine?)")
            bus = bus_manager.bus
            if bus is None:
                raise RuntimeError("I2C bus not available")

            # Wake up MPU6050
            bus.write_byte_data(self.address, 0x6B, 0)

            # Load persistent calibration
            self._load_calibration()

            self.initialized = True
            self.last_time = time.time()
            logger.info(f"PhysicalIMU: Initialized MPU6050 at {hex(self.address)}. Calibrated: {self._is_calibrated}")
        except Exception as e:
            logger.error(f"PhysicalIMU: Initialization failed: {e}")
            raise

    def shutdown(self):
        self.initialized = False
        logger.info("PhysicalIMU: Shutdown")

    def _load_calibration(self):
        if os.path.exists(CALIB_FILE):
            try:
                with open(CALIB_FILE, "r") as f:
                    self.offsets = json.load(f)
                self._is_calibrated = True
                logger.info("PhysicalIMU: Loaded calibration offsets from %s", CALIB_FILE)
            except Exception as e:
                logger.warning("PhysicalIMU: Failed to load calibration: %s", e)

    def _save_calibration(self):
        try:
            os.makedirs(os.path.dirname(CALIB_FILE), exist_ok=True)
            with open(CALIB_FILE, "w") as f:
                json.dump(self.offsets, f)
            logger.info("PhysicalIMU: Saved calibration offsets to %s", CALIB_FILE)
        except Exception as e:
            logger.error("PhysicalIMU: Failed to save calibration: %s", e)

    def calibrate(self) -> bool:
        """
        Perform zero-gyro calibration.
        Samples raw data while stationary to determine mean bias.
        """
        if not self.initialized:
            return False

        logger.info("PhysicalIMU: Starting calibration... KEEP ROBOT STILL")
        samples = []
        num_samples = 200

        try:
            for i in range(num_samples):
                raw = self._read_raw()
                if raw is None:
                    logger.error("PhysicalIMU: Calibration failed - I2C read error")
                    return False
                samples.append((raw["gx"], raw["gy"], raw["gz"]))
                time.sleep(0.01)

            # Calculate means
            gx_sum = sum(s[0] for s in samples)
            gy_sum = sum(s[1] for s in samples)
            gz_sum = sum(s[2] for s in samples)

            self.offsets = {
                "gx": gx_sum / num_samples,
                "gy": gy_sum / num_samples,
                "gz": gz_sum / num_samples
            }
            self._is_calibrated = True
            self._save_calibration()

            logger.info("PhysicalIMU: Calibration complete. Offsets: %s", self.offsets)
            return True
        except Exception as e:
            logger.error("PhysicalIMU: Calibration error: %s", e)
            return False

    def _read_raw(self) -> Optional[dict]:
        bus = bus_manager.bus
        if bus is None: return None

        try:
            # Read 14 bytes: Accel(6), Temp(2), Gyro(6)
            data = bus.read_i2c_block_data(self.address, 0x3B, 14)

            def combine(high, low):
                val = (high << 8) | low
                return val - 65536 if val > 32767 else val

            ax = combine(data[0], data[1]) / 16384.0
            ay = combine(data[2], data[3]) / 16384.0
            az = combine(data[4], data[5]) / 16384.0

            gx = combine(data[8], data[9]) / 131.0
            gy = combine(data[10], data[11]) / 131.0
            gz = combine(data[12], data[13]) / 131.0

            return {
                "ax": ax, "ay": ay, "az": az,
                "gx": gx, "gy": gy, "gz": gz
            }
        except Exception as e:
            logger.error("PhysicalIMU raw read error: %s", e)
            return None

    def get_data(self) -> dict:
        bus = bus_manager.bus
        if bus is None or not self.initialized:
            return {"accel": {"x":0,"y":0,"z":0}, "gyro": {"x":0,"y":0,"z":0}, "heading": 0.0}

        raw = self._read_raw()
        if raw is None:
            return {"accel": {"x":0,"y":0,"z":0}, "gyro": {"x":0,"y":0,"z":0}, "heading": 0.0}

        # Apply Bias Compensation
        gx = raw["gx"] - self.offsets["gx"]
        gy = raw["gy"] - self.offsets["gy"]
        gz = raw["gz"] - self.offsets["gz"]

        now = time.time()
        dt = min(now - self.last_time, 0.2)
        self.last_time = now

        # Heading integration (Yaw)
        self.yaw = (self.yaw + gz * dt) % 360.0

        return {
            "accel": {"x": raw["ax"], "y": raw["ay"], "z": raw["az"]},
            "gyro": {"x": gx, "y": gy, "z": gz},
            "heading": round(self.yaw, 2),
            "pitch": 0.0, # Simplified
            "roll": 0.0
        }

    def get_heading(self) -> float:
        return self.get_data()["heading"]

    def get_orientation(self) -> dict:
        data = self.get_data()
        return {"pitch": data["pitch"], "roll": data["roll"], "yaw": data["heading"]}

    def get_acceleration(self) -> dict:
        return self.get_data()["accel"]

    def get_angular_velocity(self) -> dict:
        return self.get_data()["gyro"]

    def status(self) -> dict:
        return {
            "initialized": self.initialized,
            "calibrated": self._is_calibrated,
            "mode": "physical"
        }
