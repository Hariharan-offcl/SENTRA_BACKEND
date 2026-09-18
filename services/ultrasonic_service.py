import time
import threading
import logging

logger = logging.getLogger(__name__)

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

FRONT_TRIG = 24
FRONT_ECHO = 25

REAR_TRIG = 5
REAR_ECHO = 6

MAX_DIST = 2.0
_h = None

def _init_hardware():
    global _h
    if not _LGPIO_AVAILABLE:
        logger.warning("lgpio not found. Ultrasonic real hardware disabled.")
        return
    try:
        from services.motor_service import _h as motor_h
        _h = motor_h
        
        if _h is None:
            logger.error("Motor service did not initialize lgpio chip.")
            return

        lgpio.gpio_claim_output(_h, FRONT_TRIG, 0)
        lgpio.gpio_claim_input(_h, FRONT_ECHO)
        
        lgpio.gpio_claim_output(_h, REAR_TRIG, 0)
        lgpio.gpio_claim_input(_h, REAR_ECHO)
        logger.info("SENTRA ultrasonic hardware initialized on Pi 5 (lgpio)")
    except Exception as e:
        logger.error(f"Failed to init ultrasonic lgpio: {e}")
        _h = None

def measure_distance(trig, echo):
    if _h is None:
        return MAX_DIST

    try:
        lgpio.gpio_write(_h, trig, 1)
        time.sleep(0.00001)
        lgpio.gpio_write(_h, trig, 0)

        start = time.monotonic()
        while not lgpio.gpio_read(_h, echo):
            if time.monotonic() - start > 0.03:
                return MAX_DIST

        pulse_start = time.monotonic()
        while lgpio.gpio_read(_h, echo):
            if time.monotonic() - pulse_start > 0.03:
                return MAX_DIST

        pulse_end = time.monotonic()
        distance = (pulse_end - pulse_start) * 343.0 / 2.0
        return min(distance, MAX_DIST)
    except Exception as e:
        logger.error(f"Ultrasonic read error: {e}")
        return MAX_DIST

def _ultrasonic_loop():
    from services.telemetry_service import _sim
    while True:
        if _h is not None:
            front = measure_distance(FRONT_TRIG, FRONT_ECHO)
            time.sleep(0.05) # Prevent signal overlap
            rear = measure_distance(REAR_TRIG, REAR_ECHO)
            
            # Update the global telemetry state
            _sim["ultrasonic"]["front_distance_m"] = round(front, 2)
            _sim["ultrasonic"]["rear_distance_m"] = round(rear, 2)
            
        time.sleep(0.2) # Update 5 times a second

def start_monitoring():
    _init_hardware()
    threading.Thread(target=_ultrasonic_loop, daemon=True).start()

