import time
import threading
import logging

from hardware.manager import hardware_manager

logger = logging.getLogger(__name__)

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

