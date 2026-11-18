import time
import threading
import logging

from hardware.manager import hardware_manager

logger = logging.getLogger(__name__)

_h = None
ULTRASONIC_FRONT_TRIGGER_GPIO = 24
ULTRASONIC_FRONT_ECHO_GPIO = 25
ULTRASONIC_REAR_TRIGGER_GPIO = 5
ULTRASONIC_REAR_ECHO_GPIO = 6


def _init_hardware() -> None:
    """Compatibility initializer that claims ultrasonic pins on the shared GPIO handle."""
    global _h
    from core.simulation import activate_once
    activate_once()
    from core import config as core_config
    if core_config.SIMULATION:
        _h = None
        return
    try:
        from hardware.gpio_manager import gpio_manager
        h = gpio_manager.chip
        if h is None:
            _h = None
            return
        ok = (
            gpio_manager.claim_output(ULTRASONIC_FRONT_TRIGGER_GPIO, owner="ultrasonic.service", initial=0)
            and gpio_manager.claim_input(ULTRASONIC_FRONT_ECHO_GPIO, owner="ultrasonic.service")
            and gpio_manager.claim_output(ULTRASONIC_REAR_TRIGGER_GPIO, owner="ultrasonic.service", initial=0)
            and gpio_manager.claim_input(ULTRASONIC_REAR_ECHO_GPIO, owner="ultrasonic.service")
        )
        _h = h if ok else None
    except Exception as exc:
        logger.warning("Ultrasonic GPIO compatibility init failed: %s", exc)
        _h = None

def _ultrasonic_loop():
    from services.telemetry_service import _US_SIM
    
    # Wait for HAL to be ready
    while getattr(hardware_manager, "ultrasonic_front", None) is None:
        time.sleep(0.5)
        
    front_sensor = hardware_manager.ultrasonic_front
    rear_sensor = hardware_manager.ultrasonic_rear
    
    while True:
        try:
            front = front_sensor.read_distance()
            rear = rear_sensor.read_distance()
            
            _US_SIM["front_distance_m"] = round(front, 3)
            _US_SIM["rear_distance_m"]  = round(rear, 3)
            _US_SIM["last_read"]        = time.time()
        except Exception as e:
            logger.error(f"Ultrasonic read error: {e}")
            
        time.sleep(0.2)   # 5 Hz update rate

def start_monitoring():
    threading.Thread(target=_ultrasonic_loop, daemon=True).start()

