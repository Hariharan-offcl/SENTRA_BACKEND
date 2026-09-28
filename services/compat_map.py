"""
SENTRA — Final Flutter app compatibility mapping (Phase 21).

Translates internal backend state into the shapes of the app's final API spec
(docs §14 in the app spec: robot objects, alert objects, people, locations,
routes) and back. Used by routers/compat.py and ws_handlers/compat_ws.py.

Internal contracts (APIS.md, Phases 0-20) are untouched — this is a pure
adapter layer. IDs are synthetic and stable:
    robot    "sentra-01" (SENTRA_COMPAT_ROBOT_ID)
    location "loc-{tag_id}"
    route    "route-{name}"
    person   person_key from the registry
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from core.state import get_robot_state
from config import settings

ROBOT_ID = os.getenv("SENTRA_COMPAT_ROBOT_ID", "sentra-01")

# internal mode → app-facing mode (app spec §3a)
MODE_TO_APP = {
    "STANDBY": "idle",
    "MANUAL": "manual",
    "PATROL": "patrol",
    "NAVIGATION": "auto",
    "FOLLOW_PERSON": "auto",
    "RETURN_TO_DOCK": "docking",
    "EMERGENCY_STOP": "idle",
}
# app-facing mode → internal mode ("mapping" drives manually like manual)
APP_TO_MODE = {
    "idle": "STANDBY",
    "manual": "MANUAL",
    "mapping": "MANUAL",
    "patrol": "PATROL",
    "auto": "NAVIGATION",
    "docking": "RETURN_TO_DOCK",
}

# internal safety event type → app alert type (app spec §12)
EVENT_TO_ALERT_TYPE = {
    "FALL": "fall",
    "PERSON_UNKNOWN": "unknown_person",
    "PERSON": "unknown_person",
    "OBSTACLE": "obstacle",
    "CLIFF": "obstacle",
    "ESTOP": "estop",
    "TIMEOUT": "connection",
    "MODE_MISMATCH": "connection",
}

# internal severity → app severity
SEV_TO_APP = {"DANGER": "critical", "WARNING": "warning", "INFO": "info"}
APP_ROLE_TO_INTERNAL = {"admin": "OWNER", "user": "GUARD"}
INTERNAL_ROLE_TO_APP = {"OWNER": "admin", "GUARD": "user", "GUEST": "user"}


def iso(epoch: float | None) -> str | None:
    """Epoch seconds → ISO-8601 UTC 'Z' string (app spec timestamps)."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def app_mode() -> str:
    return MODE_TO_APP.get(get_robot_state().get_mode(), "idle")


def battery_percent() -> float:
    from services import telemetry_service
    return float(telemetry_service._sim["battery_level"])


def current_location_name() -> str | None:
    from services import localization_service
    last = localization_service.get_localization().get("last_known")
    return (last or {}).get("name") or None


def robot_object() -> dict:
    """App spec §3a robot object for this (single) unit."""
    st = get_robot_state()
    return {
        "id": ROBOT_ID,
        "name": settings.unit_name,
        "status": "online",
        "battery_level": battery_percent(),
        "current_mode": app_mode(),
        "last_seen": iso(time.time()),
    }


def user_object(device_id: str, app_role: str, robot_id: str | None = None,
                username: str | None = None) -> dict:
    return {
        "id": device_id,
        "username": username or device_id,
        "role": app_role,
        "robot_id": robot_id,
    }


def location_object(tag: dict) -> dict:
    """tag_map entry → app location object (spec §7a)."""
    return {
        "id": f"loc-{tag['tag_id']}",
        "tag_id": tag["tag_id"],
        "name": tag["name"],
        "description": tag.get("notes", "") or "",
        "created_at": iso(tag.get("created_at")),
    }


def route_object(name: str, waypoints: list[str]) -> dict:
    """patrol route (name-keyed) → app route object (spec §8a)."""
    # Waypoints are stored as tag names; the app speaks in location ids.
    from services import tag_map
    ids = []
    for wp in waypoints:
        tag = tag_map.find_by_name(wp) if wp else None
        ids.append(f"loc-{tag['tag_id']}" if tag else wp)
    return {
        "id": f"route-{name}",
        "name": name,
        "waypoints": ids,
        "is_looping": True,  # patrol engine iterates waypoints continuously
    }


def person_object(record: dict) -> dict:
    """person_registry record → app person object (spec §13a)."""
    snap = record.get("snapshot_path")
    return {
        "id": record["person_key"],
        "name": record["name"],
        "notes": record.get("notes", "") or "",
        "registered_at": iso(record.get("created_at")),
        "image_url": f"/media/faces/{os.path.basename(snap)}" if snap else None,
    }


def alert_object(n: dict) -> dict:
    """notification dict → app alert object (spec §12a)."""
    etype = n.get("event_type", "")
    atype = EVENT_TO_ALERT_TYPE.get(etype, "connection")
    if atype == "connection":
        # Unknown event types (e.g. MANUAL/test notifications): derive from
        # the title's keywords, else fall back to "connection".
        title = (n.get("title") or "").lower()
        if "fall" in title:
            atype = "fall"
        elif "unknown person" in title or "unrecognized" in title:
            atype = "unknown_person"
        elif "obstacle" in title or "cliff" in title or "hazard" in title:
            atype = "obstacle"
        elif "stop" in title:
            atype = "estop"
        elif "battery" in title:
            atype = "battery"
    return {
        "id": n["id"],
        "type": atype,
        "severity": SEV_TO_APP.get(n.get("severity", "INFO"), "info"),
        "robot_id": ROBOT_ID,
        "robot_name": settings.unit_name,
        "timestamp": iso(n.get("created_at")) or n.get("timestamp"),
        "description": n.get("description", ""),
        "location": n.get("location"),
        "confidence": None,
        "status": "dismissed" if n.get("acknowledged") else "active",
        "image_url": None,
    }


def emergency_object(n: dict) -> dict:
    """notification dict → app `emergency` event data (spec §12)."""
    return {
        "type": EVENT_TO_ALERT_TYPE.get(n.get("event_type", ""), "connection"),
        "location": n.get("location"),
        "message": n.get("title", "") + " — " + (n.get("description") or ""),
    }


def telemetry_event() -> dict:
    """App spec §4 telemetry event data."""
    st = get_robot_state()
    return {
        "battery_percent": battery_percent(),
        "mode": app_mode(),
        "connection_status": "connected",
        "is_emergency": bool(st.is_estop_active()),
        "current_location": current_location_name(),
    }


def sensor_event() -> dict:
    """App spec §6 sensor_update event data (2 Hz)."""
    from services import sensor_service
    snap = sensor_service.get_snapshot()
    imu = snap.get("imu") or {}
    enc = (snap.get("wheel_encoders") or {}).get("speed_mps") or {}
    g = 9.81  # internal IMU reports g; the app expects m/s²
    return {
        "front_distance": snap.get("front_distance_m"),
        "rear_distance": snap.get("rear_distance_m"),
        "cliff_sensors": [bool(snap.get("left_cliff")),
                          bool(snap.get("right_cliff")), False, False],
        "imu": {
            "pitch": imu.get("pitch", 0.0),
            "roll": imu.get("roll", 0.0),
            "yaw": imu.get("yaw", 0.0),
            "accel_x": (imu.get("ax", 0.0) or 0.0) * g,
            "accel_y": (imu.get("ay", 0.0) or 0.0) * g,
            "accel_z": (imu.get("az", 0.0) or 0.0) * g,
        },
        "left_encoder_speed": enc.get("left_mps", 0.0),
        "right_encoder_speed": enc.get("right_mps", 0.0),
    }


def apriltag_event(det: dict) -> dict:
    """App spec §7 apriltag_detected event data."""
    return {
        "tag_id": det.get("tag_id"),
        "confidence": round(det.get("confidence") or 0.0, 3),
        "distance": det.get("distance_m"),
        "bearing": det.get("bearing_deg"),
    }


def person_event(track: dict) -> dict:
    """App spec §11 person_detected event data (body tracks are un-identified)."""
    return {
        "is_known": False,
        "person_id": str(track.get("person_id")),
        "name": None,
        "confidence": track.get("confidence"),
        "image_url": None,
    }


_NAV_STATE_TO_APP = {
    "SEEK": "searching_tag",
    "APPROACH": "moving",
    "ALIGN": "moving",
    "ARRIVED": "arrived",
    "DOCKED": "arrived",
    "ABORT": "error",
}


def navigation_event() -> dict:
    """App spec §8 navigation_status from nav / patrol / dock sessions."""
    app = {"status": "idle", "current_location": current_location_name(),
           "next_waypoint": None, "progress_percent": 0.0}

    from services import navigation_service
    nav = navigation_service.status()
    if nav:
        app["status"] = _NAV_STATE_TO_APP.get(nav.get("state", ""), "moving")
        app["next_waypoint"] = nav.get("target")
        return app

    from services import patrol_service
    ps = patrol_service.status()
    sess = ps.get("session")
    if ps.get("active") and sess:
        wps = sess.get("waypoints") or []
        idx = sess.get("index", 0)
        app["status"] = "blocked" if sess.get("blocked") else "moving"
        app["next_waypoint"] = sess.get("current_waypoint") or (
            wps[idx] if idx < len(wps) else None)
        app["progress_percent"] = round(100.0 * idx / len(wps), 1) if wps else 0.0
        return app

    from services import docking_service
    dock = docking_service.status()
    if dock:
        app["status"] = _NAV_STATE_TO_APP.get(dock.get("state", ""), "moving")
        app["next_waypoint"] = "Dock"
        return app
    return app


def camera_status() -> dict:
    """App spec §11 camera status (rover phone is the only vision source)."""
    from services import vision_service
    try:
        stats = vision_service.stats()
    except Exception:
        stats = {}
    streaming = bool(stats.get("frames_seen", 0) > 0)
    return {"is_streaming": streaming, "stream_url": None}
