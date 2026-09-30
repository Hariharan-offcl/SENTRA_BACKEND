import logging
import threading
import time

from hardware.manager import hardware_manager

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_current_data = {"voltage": 0.0, "current": 0.0, "power": 0.0}
_last_read = 0.0
_enabled = True
_stop_event = threading.Event()
_thread = None

def _loop():
    global _last_read
    while not _stop_event.is_set():
        try:
            data = hardware_manager.current_sensor.read()
            with _lock:
                _current_data.update(data)
                _last_read = time.time()
        except Exception as exc:
            logger.error("current sensor error: %s", exc)
        _stop_event.wait(0.1)

def start():
    global _thread
    if not _enabled:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="sentra-current", daemon=True)
    _thread.start()

def stop():
    _stop_event.set()

def sensor_provider() -> dict:
    with _lock:
        data = dict(_current_data)
        last_read = _last_read
    return {
        "current": data,
        "current_enabled": _enabled,
        "current_simulated": hardware_manager.current_sensor.status().get("mode") == "simulated",
        "current_last_read": last_read
    }
