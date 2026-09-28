import logging
from core.config import settings
from hardware.motor.physical import PhysicalMotor
from hardware.motor.simulated import SimulatedMotor
from hardware.ultrasonic.physical import PhysicalUltrasonic
from hardware.ultrasonic.simulated import SimulatedUltrasonic
from hardware.imu.physical import PhysicalIMU
from hardware.imu.simulated import SimulatedIMU
from hardware.current_sensor.physical import PhysicalCurrentSensor
from hardware.current_sensor.simulated import SimulatedCurrentSensor
from hardware.cliff.physical import PhysicalCliffSensor
from hardware.cliff.simulated import SimulatedCliffSensor
from hardware.encoder.physical import PhysicalEncoder
from hardware.encoder.simulated import SimulatedEncoder

logger = logging.getLogger("sentra.hardware.manager")

class HardwareManager:
    """
    The Orchestrator for the SENTRA Hardware Abstraction Layer (HAL).
    Provides a single point of access to all physical/simulated hardware.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _setup_components(self):
        # We use a simulated mode if configured in settings
        # Note: In a real scenario, this would come from core_config.SIMULATION
        # Since we are in a transition, we check settings or env
        sim = False # Default to physical for this HAL; can be toggled

        # Motor
        self.motor = SimulatedMotor() if sim else PhysicalMotor()

        # Ultrasonic - we need two sensors
        self.ultrasonic_front = SimulatedUltrasonic("FRONT") if sim else PhysicalUltrasonic("FRONT", 24, 25)
        self.ultrasonic_rear = SimulatedUltrasonic("REAR") if sim else PhysicalUltrasonic("REAR", 5, 6)

        # IMU
        self.imu = SimulatedIMU() if sim else PhysicalIMU()

        # Current Sensor
        self.current_sensor = SimulatedCurrentSensor() if sim else PhysicalCurrentSensor()

        # Cliff Sensors
        self.cliff = SimulatedCliffSensor() if sim else PhysicalCliffSensor()

        # Encoder
        self.encoder = SimulatedEncoder() if sim else PhysicalEncoder()

    def initialize(self):
        if self._initialized:
            return

        logger.info("HardwareManager: Initializing HAL components...")
        self._setup_components()

        # Ordered initialization
        self.motor.initialize()
        self.ultrasonic_front.initialize()
        self.ultrasonic_rear.initialize()
        self.imu.initialize()
        self.current_sensor.initialize()
        self.cliff.initialize()
        self.encoder.initialize()

        self._initialized = True
        logger.info("HardwareManager: HAL successfully initialized")

    def shutdown(self):
        if not self._initialized:
            return

        logger.info("HardwareManager: Shutting down HAL components...")
        self.motor.shutdown()
        self.ultrasonic_front.shutdown()
        self.ultrasonic_rear.shutdown()
        self.imu.shutdown()
        self.current_sensor.shutdown()
        self.cliff.shutdown()
        self.encoder.shutdown()

        self._initialized = False
        logger.info("HardwareManager: HAL shutdown complete")

# Singleton instance
hardware_manager = HardwareManager()
