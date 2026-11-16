"""Hardware independent regression checks for graduated sensor fusion."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import sensor_fusion


def decide(sensors, people=(), tags=()):
    with patch("core.safety.get_safety_layer") as safety, \
         patch("services.person_detection.get_tracked", return_value=list(people)), \
         patch("services.apriltag_service.get_recent", return_value=list(tags)):
        safety.return_value.read_sensors.return_value = sensors
        return sensor_fusion.get_speed_decision(80, 80)


def main():
    assert decide({"front_distance_m": 0.7})[:2] == (40.0, 40.0)
    assert decide({"front_distance_m": 0.35})[:2] == (16.0, 16.0)
    assert decide({"front_distance_m": 0.15})[:2] == (0.0, 0.0)
    person = {"confidence": .9, "age_s": .2}
    assert decide({"front_distance_m": .7}, [person])[:2] == (32.0, 32.0)
    left, right, *_ = decide({"left_cliff": True})
    assert left <= 0 and right >= 0 and (left, right) != (80, 80)
    # The fusion stage only caps outputs; the centralized gate still rejects
    # movement when E-stop is latched.
    from core.safety import SafetyLayer
    from core.state import get_robot_state
    layer = SafetyLayer()
    layer.wire(lambda *_: None, lambda: {})
    state = get_robot_state()
    previous = state.is_estop_active()
    try:
        state.trigger_estop(reason="phase22_test")
        result = layer.check_and_apply(owner="test", mode=state.get_mode(),
                                       left_pct=10, right_pct=10)
        assert not result.get("applied")
    finally:
        if not previous:
            state.reset_estop()
    print("Phase 22 sensor fusion checks passed")


if __name__ == "__main__":
    main()
