import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_event_bus_deduplicates_and_bounds_history():
    from services import event_bus

    first = event_bus.publish("FALL", {"person_id": 7}, dedupe_key="fall-7", dedupe_s=60)
    second = event_bus.publish("FALL", {"person_id": 7}, dedupe_key="fall-7", dedupe_s=60)

    assert first["deduplicated"] is not True
    assert second["deduplicated"] is True
    assert event_bus.history(10)
    assert event_bus.stats()["history_size"] <= 500


def test_event_bus_does_not_retain_non_deduplicated_frame_keys():
    from services import event_bus

    for frame_number in range(1200):
        event_bus.publish("FRAME_SAMPLED", {"sequence": frame_number})

    assert event_bus.stats()["dedupe_keys"] <= 1000


def test_demo_state_has_separate_state_domains():
    from services import demo_state

    state = demo_state.snapshot()
    assert {"operation", "mission", "navigation", "detection", "call", "timestamp"} <= set(state)
    assert {"mode", "mode_owner", "estop_active", "motors_disabled", "moving"} <= set(state["operation"])


def test_connection_manager_tracks_heartbeat_age():
    from services.connection_manager import ConnectionManager

    manager = ConnectionManager()
    websocket = object()
    manager.active_connections[websocket] = {
        "role": "ROVER",
        "user_id": "rover-1",
        "last_heartbeat": 0.0,
    }
    stale = manager.stale_heartbeats(max_age_s=5.0)
    assert stale == []

    import time
    manager.active_connections[websocket]["last_heartbeat"] = time.time() - 10
    stale = manager.stale_heartbeats(max_age_s=5.0)
    assert stale and stale[0]["role"] == "ROVER"


def test_rover_heartbeat_is_broadcast_to_user_clients(monkeypatch):
    import asyncio
    from ws_handlers import app_ws

    sent = []
    broadcast = []

    async def capture_send(_websocket, event, data):
        sent.append((event, data))

    monkeypatch.setattr(app_ws, "send_event", capture_send)
    monkeypatch.setattr(
        app_ws,
        "push_event",
        lambda event, data: broadcast.append((event, data)),
    )
    asyncio.run(app_ws._handle_heartbeat(object(), {
        "mode": "PATROL",
        "camera_state": "ACTIVE",
        "current_command": "PATROL",
    }, is_rover=True))

    assert sent == [("heartbeat_ack", {})]
    assert broadcast[0][0] == "heartbeat_ack"
    assert broadcast[0][1]["mode"] == "PATROL"
    assert broadcast[0][1]["camera_state"] == "ACTIVE"


def test_robot_status_uses_rover_heartbeat_freshness(monkeypatch):
    import time
    from services import compat_map
    from ws_handlers import app_ws

    monkeypatch.setitem(app_ws._rover_state, "last_heartbeat", time.time() - 10)
    assert compat_map.robot_object()["status"] == "offline"
    monkeypatch.setitem(app_ws._rover_state, "last_heartbeat", time.time())
    assert compat_map.robot_object()["status"] == "online"


def test_compat_call_lifecycle_deduplicates_active_calls():
    import time
    import routers.compat as compat
    compat._calls.clear()
    now = time.time()
    call = {"id": "call-test", "robot_id": "sentra-01", "status": "ringing", "started_at": now}
    compat.register_call(call)
    compat.register_call({
        "id": "call-test-duplicate",
        "robot_id": "sentra-01",
        "status": "ringing",
        "started_at": now + 1,
    })
    assert len(compat._calls) == 1
    compat._cancel_call_expiry("call-test")
    compat._calls.clear()


def test_call_events_include_contract_ids_and_expire_ringing_calls(monkeypatch):
    import time
    import routers.compat as compat
    from ws_handlers import compat_ws

    pushed = []
    monkeypatch.setattr(compat_ws, "push_event", lambda event, data: pushed.append((event, data)))
    compat._calls.clear()
    call = {
        "id": "call-expiring",
        "robot_id": "sentra-01",
        "status": "ringing",
        "call_type": "emergency",
        "source_alert_id": "alert-1",
        "started_at": time.time(),
    }
    compat.register_call(call)
    assert pushed[-1][0] == "call_incoming"
    assert pushed[-1][1]["id"] == "call-expiring"
    assert pushed[-1][1]["call_type"] == "emergency"

    compat._expire_ringing_calls(
        now=call["started_at"] + compat._CALL_RING_TIMEOUT_S
    )
    assert call["status"] == "missed"
    assert pushed[-1][0] == "call_ended"
    assert pushed[-1][1]["status"] == "missed"
    compat._cancel_call_expiry("call-expiring")
    compat._calls.clear()


def test_rest_call_accept_and_end_publish_lifecycle_events(monkeypatch):
    import json
    import time
    import routers.compat as compat
    from ws_handlers import compat_ws

    pushed = []
    monkeypatch.setattr(compat_ws, "push_event", lambda event, data: pushed.append((event, data)))
    compat._calls.clear()
    call = {
        "id": "call-lifecycle",
        "robot_id": "sentra-01",
        "status": "ringing",
        "call_type": "manual",
        "started_at": time.time(),
    }
    compat.register_call(call)

    accepted = json.loads(compat.call_accept("call-lifecycle").body)
    assert accepted["status"] == "active"
    assert pushed[-1][0] == "call_accepted"
    assert pushed[-1][1]["callId"] == "call-lifecycle"

    ended = json.loads(compat.call_end("call-lifecycle").body)
    assert ended["status"] == "ended"
    assert pushed[-1][0] == "call_ended"
    assert pushed[-1][1]["status"] == "ended"
    compat._cancel_call_expiry("call-lifecycle")
    compat._calls.clear()
