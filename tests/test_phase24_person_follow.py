"""Direct, hardware-free checks for follow navigation handlers."""
import os
import sys
import time
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Keep service imports independent of Pi-only Pydantic settings and hardware.
motor_stub = types.ModuleType("services.motor_service")
motor_stub.stop_all = lambda *_a, **_k: None
motor_stub.apply_wheel_speeds = lambda *_a, **_k: {"applied": True}
sys.modules.setdefault("services.motor_service", motor_stub)
patrol_stub = types.ModuleType("services.patrol_service")
patrol_stub.stop_patrol = lambda *_a, **_k: None
sys.modules.setdefault("services.patrol_service", patrol_stub)
from core.state import STANDBY, get_robot_state
get_robot_state().request_mode(STANDBY, requested_by="phase24_test")
from core import config
from core.state import NAVIGATION
from services import navigation_service as nav


def main():
    nav.cancel("phase24_reset")
    nav._session = {"id": "test-follow", "state": "PERSON_REACHED",
                    "target": "kitchen", "person_lost_since": None,
                    "started_at": time.time(), "state_started_at": time.time()}
    with patch("services.motor_service.stop_all"):
        nav._state_person_reached(nav.status())
    assert nav.status()["state"] == "FOLLOWING"

    person = {"bbox": [280, 100, 80, 180], "frame_width": 640,
              "age_s": .1, "confidence": .9}
    applied = []
    with patch.object(nav, "_get_tracked_person", return_value=person), \
         patch("core.safety.get_safety_layer") as safety, \
         patch("services.motor_service.apply_wheel_speeds",
               side_effect=lambda l, r, **kw: applied.append((l, r)) or {"applied": True}), \
         patch("services.motor_service.stop_all"):
        safety.return_value.read_sensors.return_value = {"front_distance_m": 1.5}
        nav._state_following(nav.status())
    assert applied and applied[-1] == (20.0, 20.0), applied

    applied.clear()
    with patch.object(nav, "_get_tracked_person", return_value=person), \
         patch("core.safety.get_safety_layer") as safety, \
         patch("services.motor_service.apply_wheel_speeds",
               side_effect=lambda l, r, **kw: applied.append((l, r)) or {"applied": True}), \
         patch("services.motor_service.stop_all") as stop:
        safety.return_value.read_sensors.return_value = {"front_distance_m": .3}
        nav._state_following(nav.status())
        stop.assert_called_with("follow_too_close")
    assert not applied

    nav._session["person_lost_since"] = None
    with patch.object(nav, "_get_tracked_person", return_value=None), \
         patch("services.motor_service.stop_all"):
        nav._state_following(nav.status())
    assert nav.status()["state"] == "FOLLOWING"
    assert nav.status()["person_lost_since"] is not None
    nav._session["person_lost_since"] = time.time() - config.FOLLOW_LOST_TIMEOUT_S - .2
    with patch.object(nav, "_get_tracked_person", return_value=None), \
         patch("services.motor_service.stop_all"):
        nav._state_following(nav.status())
    assert nav.status()["state"] == "PERSON_SEARCH"

    # E-stop and voice STOP both cancel the follow session via existing public
    # cancellation paths; reset the global simulator latch afterward.
    state = get_robot_state()
    state.request_mode(NAVIGATION, requested_by="navigation_service")
    state.trigger_estop(reason="phase24_test")
    nav._engine_stop.clear()
    from threading import Thread
    engine = Thread(target=nav._engine_loop, daemon=True)
    engine.start()
    deadline = time.time() + 1.0
    while nav.status() is not None and time.time() < deadline:
        time.sleep(.01)
    assert nav.status() is None
    nav._engine_stop.set()
    engine.join(timeout=1.0)
    state.reset_estop()
    nav._session = {"id": "test-follow-stop", "state": "FOLLOWING",
                    "started_at": time.time(), "state_started_at": time.time()}
    from services import voice_service
    with patch("services.patrol_service.stop_patrol"), \
         patch("services.docking_service.cancel_return"), \
         patch("services.docking_service.start_return", return_value={"ok": False}), \
         patch("services.motor_service.stop_all"):
        result = voice_service.execute("STOP")
    assert result["handled"] and nav.status() is None
    print("Phase 24 person-follow checks passed")


if __name__ == "__main__":
    main()
