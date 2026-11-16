"""Voice command chaining and aliases without hardware access."""
import os
import sys
import time
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
motor_stub = types.ModuleType("services.motor_service")
motor_stub.stop_all = lambda *_a, **_k: None
sys.modules.setdefault("services.motor_service", motor_stub)
from services import voice_service
from services import navigation_service as nav


def main():
    tag = {"tag_id": 7, "name": "kitchen"}
    order = []
    with patch.object(voice_service, "resolve_target", return_value=tag), \
         patch("services.docking_service.dock_tag_id", return_value=1), \
         patch("services.apriltag_service.get_last_seen", return_value={"timestamp": 9999999999}), \
         patch("services.docking_service.undock", side_effect=lambda: order.append("undock") or {"ok": True}), \
         patch("services.navigation_service.go_to",
               side_effect=lambda *a, **kw: order.append(("navigate", kw)) or
               {"ok": True, "session": {"state": "SEEK"}}):
        result = voice_service.execute("GO_TO", location="kitchen")
    assert result["handled"] and order[0] == "undock" and order[1][0] == "navigate", order
    assert order[1][1].get("search_person") is True

    # Come-to and move-to API enum aliases share the same navigation dispatcher.
    for alias in ("COME_TO", "MOVE_TO"):
        with patch.object(voice_service, "resolve_target", return_value=tag), \
             patch("services.docking_service.dock_tag_id", return_value=None), \
             patch("services.navigation_service.go_to",
                   return_value={"ok": True, "session": {"state": "SEEK"}}) as go:
            out = voice_service.execute(alias, location="kitchen")
            assert out["handled"]
            go.assert_called_once()

    # Final arrival on a voice route retains the session and starts search.
    nav._session = {
        "id": "phase25-arrival", "state": "APPROACH", "target": "kitchen",
        "tag_id": 7, "final_target": "kitchen", "search_person": True,
        "started_at": time.time(), "state_started_at": time.time(),
        "blocked_since": None, "hops": [{"name": "kitchen", "tag_id": 7}],
        "hop_index": 0, "completed_hops": [], "prev_node": "hallway",
    }
    with patch("services.route_graph.observe_traversal"):
        nav._arrived(nav.status())
    assert nav.status()["state"] == "PERSON_SEARCH"
    nav.cancel("phase25_cleanup")
    print("Phase 25 voice-chain checks passed")


if __name__ == "__main__":
    main()
