import logging

from core import config as core_config
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

    Phase 0 repair:
      * reads core_config.SIMULATION instead of the old hardcoded `sim = False`;
      * a physical component whose initialize() fails falls back to its
        simulated counterpart instead of crashing the whole backend boot.
    """
    _instance = None

    # Public component slots. They start as *constructed simulated* drivers so
    # that code touching hardware_manager.<slot> before (or without) a
    # lifespan-driven initialize() — e.g. dev-box test runs and the estop path
    # in motor_service — gets safe no-op behaviour instead of AttributeError
    # or NoneType crashes. initialize() replaces them with physical drivers
    # (falling back to simulated per-slot when init fails).
    motor = SimulatedMotor()
    ultrasonic_front = SimulatedUltrasonic("FRONT")
    ultrasonic_rear = SimulatedUltrasonic("REAR")
    imu = SimulatedIMU()
    current_sensor = SimulatedCurrentSensor()
    cliff = SimulatedCliffSensor()
    encoder = SimulatedEncoder()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def _specs(self):
        """(attribute name, physical factory, simulated factory) per HAL slot."""
        return [
            ("motor", lambda: PhysicalMotor(), lambda: SimulatedMotor()),
            ("ultrasonic_front", lambda: PhysicalUltrasonic("FRONT", 24, 25),
             lambda: SimulatedUltrasonic("FRONT")),
            ("ultrasonic_rear", lambda: PhysicalUltrasonic("REAR", 5, 6),
             lambda: SimulatedUltrasonic("REAR")),
            ("imu", lambda: PhysicalIMU(), lambda: SimulatedIMU()),
            ("current_sensor", lambda: PhysicalCurrentSensor(), lambda: SimulatedCurrentSensor()),
            ("cliff", lambda: PhysicalCliffSensor(), lambda: SimulatedCliffSensor()),
            ("encoder", lambda: PhysicalEncoder(), lambda: SimulatedEncoder()),
        ]

    def initialize(self):
        if self._initialized:
            return

        logger.info("HardwareManager: Initializing HAL components (SIMULATION=%s)...",
                    core_config.SIMULATION)

        for name, physical_fn, simulated_fn in self._specs():
            instance = simulated_fn() if core_config.SIMULATION else physical_fn()
            try:
                instance.initialize()
            except Exception as exc:
                logger.error("HardwareManager: %s initialization failed (%s) "
                             "— switching to simulated", name, exc)
                try:
                    instance.shutdown()
                except Exception:
                    pass
                instance = simulated_fn()
                instance.initialize()
            setattr(self, name, instance)

        self._initialized = True
        logger.info("HardwareManager: HAL successfully initialized")

    def shutdown(self):
        if not self._initialized:
            return

        logger.info("HardwareManager: Shutting down HAL components...")
        for name, _physical_fn, _simulated_fn in self._specs():
            try:
                getattr(self, name).shutdown()
            except Exception as exc:
                logger.warning("HardwareManager: %s shutdown error: %s", name, exc)

        self._initialized = False
        logger.info("HardwareManager: HAL shutdown complete")


# Singleton instance
hardware_manager = HardwareManager()
