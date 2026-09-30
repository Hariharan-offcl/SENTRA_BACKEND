"""
SENTRA — Patrol service (Phase 7).

Route-based patrol through mapped locations (Phase 6 tag map), with:

    - Routes persisted to disk (~/sentra_data/patrol_routes.json), editable
      via the API. A route is an ordered list of location names, e.g.
          ["Dock", "Hall", "Kitchen", "Bedroom", "Hall"]
    - A cruise engine (10 Hz) that drives FORWARD through the safety gate,
      STOPS at obstacles (never steers around), and waits for a clear path.
    - Waypoint confirmation via AprilTag: a waypoint is done when its mapped
      tag is seen within the freshness window.
    - Waypoint timeout → skip with a PATROL event (route keeps going).
    - No local routes / no default → falls back to the legacy wander loop
      behaviour (Phase 1) so old behaviour is preserved.
    - Takeover safety: if any other mode takes the motors, the engine ends
      the patrol session cleanly (safety gate zeroes on mismatch).

Mode semantics: PATROL mode is owned by "patrol_service" in core.state; the
manual joystick taking over flips mode to MANUAL, which this engine detects
and respects.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from core import config as core_config
from core.safety_config import get_safety_config
from core.state import get_robot_state, PATROL, MANUAL, STANDBY
from services import safety_events

logger = logging.getLogger(__name__)

ENGINE_TICK_S = 0.2

_lock = threading.RLock()
_routes: dict[str, list[str]] = {}
_default_route: Optional[str] = None
_routes_path: str = core_config.PATROL_ROUTES_PATH

_engine_thread: threading.Thread | None = None
_engine_stop = threading.Event()
_session: Optional[dict] = None        # active patrol session
_legacy_mode_requested = False         # set when running the wander fallback


# ── Routes store ─────────────────────────────────────────────────────────────

def load_routes(path: Optional[str] = None) -> None:
    global _routes_path, _routes, _default_route
    with _lock:
        if path:
            _routes_path = path
        if os.path.exists(_routes_path):
            try:
                with open(_routes_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _routes = {str(k): list(v) for k, v in data.get("routes", {}).items()}
                _default_route = data.get("default")
                logger.info("Patrol routes loaded: %d from %s", len(_routes), _routes_path)
                return
            except Exception as exc:
                logger.error("Patrol routes load failed (%s) — starting empty", exc)
        _routes = {}
        _default_route = None


def _save_routes_locked() -> None:
    tmp = _routes_path + ".tmp"
    os.makedirs(os.path.dirname(_routes_path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"routes": _routes, "default": _default_route},
                  f, indent=2, ensure_ascii=False)
    os.replace(tmp, _routes_path)


def list_routes() -> dict[str, list[str]]:
    with _lock:
        return {k: list(v) for k, v in _routes.items()}


def get_route(name: str) -> Optional[list[str]]:
    with _lock:
        r = _routes.get(name)
        return list(r) if r else None


def save_route(name: str, waypoints: list[str], set_default: bool = False) -> dict:
    """Create/update a route. Validates waypoints against the tag map
    (unknown names are rejected — patrol must confirm via mapped tags)."""
    global _default_route
    from services import tag_map
    if not name or not name.strip():
        return {"ok": False, "error": "route name must not be empty"}
    name = name.strip()
    if not isinstance(waypoints, list) or len(waypoints) < 2:
        return {"ok": False, "error": "route needs at least 2 waypoints"}
    clean = []
    for wp in waypoints:
        if not isinstance(wp, str) or not tag_map.find_by_name(wp):
            return {"ok": False, "error": f"unknown waypoint location: {wp!r}"}
        clean.append(tag_map.find_by_name(wp)["name"])
    with _lock:
        created = name not in _routes
        _routes[name] = clean
        if set_default or _default_route is None:
            _default_route = name
        _save_routes_locked()
    logger.info("Patrol route saved: %s → %s (default=%s)", name, clean, set_default)
    return {"ok": True, "created": created, "name": name, "waypoints": clean}


def delete_route(name: str) -> dict:
    global _default_route
    with _lock:
        if name not in _routes:
            return {"ok": False, "error": "unknown route"}
        del _routes[name]
        if _default_route == name:
            _default_route = next(iter(_routes), None)
        _save_routes_locked()
    return {"ok": True}


def get_default_route() -> Optional[str]:
    with _lock:
        return _default_route


def set_default_route(name: str) -> dict:
    global _default_route
    with _lock:
        if name not in _routes:
            return {"ok": False, "error": "unknown route"}
        _default_route = name
        _save_routes_locked()
    return {"ok": True, "default": name}


def routes_path() -> str:
    return _routes_path


# ── Engine ───────────────────────────────────────────────────────────────────

def _resolve_waypoint(name: str) -> Optional[int]:
    from services import tag_map
    tag = tag_map.find_by_name(name)
    return tag["tag_id"] if tag else None


def _path_is_clear(sensors: dict) -> tuple[bool, str]:
    """Patrol path check: front obstacle or cliff blocks cruising."""
    cfg = get_safety_config()
    front = sensors.get("front_distance_m")
    if isinstance(front, (int, float)) and front < cfg.get("front_obstacle_stop_m"):
        return False, f"front obstacle {front}m"
    if sensors.get("left_cliff") or sensors.get("right_cliff"):
        return False, "cliff"
    return True, ""


def _log_event(detail: dict, severity: str = "INFO") -> None:
    try:
        safety_events.report("PATROL", detail, severity=severity)
    except Exception:
        pass


def _engine_loop() -> None:
    """Cruise engine: orchestrates navigation_service to drive the route."""
    from services.motor_service import stop_all
    from services import navigation_service, localization_service

    logger.info("Patrol engine started (tick=%.2fs)", ENGINE_TICK_S)
    while not _engine_stop.is_set():
        session = _get_session()
        if session is None:
            _engine_stop.wait(ENGINE_TICK_S)
            continue

        st = get_robot_state()
        route_name = session["route"]
        waypoints = session["waypoints"]
        from core.state import NAVIGATION

        # PAUSE logic
        if session.get("paused"):
            if st.is_estop_active() or st.get_mode() not in (PATROL, NAVIGATION):
                reason = "estop" if st.is_estop_active() else f"mode_changed_to_{st.get_mode()}"
                _end_session(reason)
                continue
            if st.get_mode() == NAVIGATION:
                navigation_service.cancel("patrol_paused")
            _engine_stop.wait(ENGINE_TICK_S)
            continue

        # Respect e-stop / manual takeover → end session
        if st.is_estop_active() or st.get_mode() not in (PATROL, NAVIGATION):
            reason = "estop" if st.is_estop_active() else f"mode_changed_to_{st.get_mode()}"
            logger.info("Patrol %s ending: %s", route_name, reason)
            if st.get_mode() == NAVIGATION:
                navigation_service.cancel("patrol_cancelled")
            _end_session(reason)
            continue

        idx = session["index"]
        if idx >= len(waypoints):
            if st.get_mode() == NAVIGATION:
                navigation_service.cancel("patrol_completed")
            logger.info("Patrol %s completed all waypoints", route_name)
            _log_event({"event": "completed", "route": route_name}, "INFO")
            _end_session("completed")
            continue

        wp_name = waypoints[idx]
        wp_tag = _resolve_waypoint(wp_name)

        nav_sess = navigation_service.status()

        # Are we there?
        if localization_service.is_at(wp_name):
            logger.info("Patrol %s: waypoint '%s' confirmed (tag %s)", route_name, wp_name, wp_tag)
            _log_event({"event": "waypoint", "route": route_name, "waypoint": wp_name, "tag_id": wp_tag, "status": "confirmed"})
            with _lock:
                if _session and _session["id"] == session["id"]:
                    _session["index"] = idx + 1
                    _session["waypoint_started_at"] = time.time()
                    _session["confirmed_waypoints"].append(wp_name)
                    _session["blocked"] = False
                    _session["blocked_since"] = None
            if nav_sess:
                navigation_service.cancel("arrived")
            if st.get_mode() != PATROL:
                st.request_mode(PATROL, requested_by="patrol_service")
            _engine_stop.wait(2.0)  # pause at the waypoint before continuing
            continue

        # Timeout guard for this waypoint
        if time.time() - session["waypoint_started_at"] > core_config.PATROL_WAYPOINT_TIMEOUT_S:
            logger.warning("Patrol %s: waypoint '%s' TIMED OUT — skipping", route_name, wp_name)
            _log_event({"event": "waypoint", "route": route_name, "waypoint": wp_name, "status": "timeout"}, severity="WARNING")
            with _lock:
                if _session and _session["id"] == session["id"]:
                    _session["index"] = idx + 1
                    _session["waypoint_started_at"] = time.time()
                    _session["skipped_waypoints"].append(wp_name)
                    _session["blocked"] = False
            if nav_sess:
                navigation_service.cancel("timeout")
            continue

        # Start navigation if it's not running
        if not nav_sess:
            if st.get_mode() in (STANDBY, PATROL):
                logger.info("Patrol delegating leg to navigation_service: go to %s (tag %s)", wp_name, wp_tag)
                if wp_tag is None:
                    # Target is completely unknown/removed, skip it
                    logger.warning("Patrol skipping unknown target '%s'", wp_name)
                    with _lock:
                        if _session and _session["id"] == session["id"]:
                            _session["index"] = idx + 1
                            _session["waypoint_started_at"] = time.time()
                            _session["skipped_waypoints"].append(wp_name)
                    continue

                res = navigation_service.go_to(wp_tag, wp_name, source="patrol")
                if not res.get("ok"):
                    logger.error("Patrol failed to start navigation: %s", res)
                    _end_session(f"navigation_failed: {res.get('error')}")
                    continue
        else:
            # Navigation is running, mirror its blocked state
            nav_state = nav_sess.get("state")
            blocked = (nav_state == "BLOCKED" or nav_state == "FAILED")
            with _lock:
                if _session and _session["id"] == session["id"]:
                    _session["blocked"] = blocked
                    if blocked and not _session.get("blocked_since"):
                        _session["blocked_since"] = time.time()
                    elif not blocked:
                        _session["blocked_since"] = None

        _engine_stop.wait(ENGINE_TICK_S)

    logger.info("Patrol engine stopped")


def get_safety_layer_sensors() -> dict:
    from core.safety import get_safety_layer
    return get_safety_layer().read_sensors()


def _get_session() -> Optional[dict]:
    with _lock:
        return dict(_session) if _session else None


def _end_session(reason: str) -> None:
    global _session, _legacy_mode_requested
    with _lock:
        if _session:
            _session["active"] = False
            _session["ended_reason"] = reason
            _session["ended_at"] = time.time()
            _session = None
        _legacy_mode_requested = False
    get_robot_state().request_mode(STANDBY, requested_by="patrol_service")


# ── Public session API ───────────────────────────────────────────────────────

def start_patrol(route: Optional[str] = None) -> dict:
    """Start a patrol session on a route (default route if unnamed).
    Falls back to the legacy wander behaviour when no routes exist."""
    global _session
    st = get_robot_state()
    if st.is_estop_active():
        return {"ok": False, "error": "estop_latched"}

    route_name = route or _default_route
    waypoints = get_route(route_name) if route_name else None

    result = st.request_mode(PATROL, requested_by="rest_api")
    if not result.get("accepted"):
        return {"ok": False, "error": f"mode_unavailable ({st.get_mode()})"}

    if waypoints:
        resolved = [(wp, _resolve_waypoint(wp)) for wp in waypoints]
        missing = [wp for wp, tid in resolved if tid is None]
        if missing:
            st.request_mode(STANDBY, requested_by="patrol_service")
            return {"ok": False, "error": f"waypoints not in tag map: {missing}"}
        with _lock:
            _session = {
                "id": f"patrol-{int(time.time())}",
                "route": route_name,
                "waypoints": waypoints,
                "index": 0,
                "active": True,
                "started_at": time.time(),
                "waypoint_started_at": time.time(),
                "blocked": False,
                "blocked_since": None,
                "paused": False,
                "paused_at": None,
                "confirmed_waypoints": [],
                "skipped_waypoints": [],
                "mode": "route",
            }
        from services.motor_service import start_patrol_loop
        start_patrol_loop()  # keep the legacy thread alive for idle/legacy use
        logger.info("Patrol session %s started on route '%s'", _session["id"], route_name)
        _log_event({"event": "started", "route": route_name,
                    "waypoints": waypoints})
        return {"ok": True, "session": _session_status()}

    # No usable route → legacy wander behaviour (Phase 1 loop drives PATROL)
    global _legacy_mode_requested
    with _lock:
        _legacy_mode_requested = True
    from services.motor_service import start_patrol_loop
    start_patrol_loop()
    logger.info("Patrol started in LEGACY wander mode (no routes defined)")
    return {"ok": True, "legacy_wander": True, "session": None}


def stop_patrol() -> dict:
    """Stop any active patrol (route or legacy)."""
    global _session, _legacy_mode_requested
    had = _session is not None or _legacy_mode_requested
    with _lock:
        if _session:
            _session["active"] = False
            _session["ended_reason"] = "stopped"
            _session["ended_at"] = time.time()
            _session = None
        _legacy_mode_requested = False
    from services.motor_service import stop_patrol_loop, stop_all
    stop_patrol_loop()
    stop_all("patrol_stop")
    st = get_robot_state()
    if st.get_mode() == PATROL:
        st.request_mode(STANDBY, requested_by="patrol_service")
    _log_event({"event": "stopped"})
    return {"ok": True, "was_active": had}


# ── Pause / resume (Phase 5, audit P10) ──────────────────────────────────────

def pause_patrol() -> dict:
    """Pause the active patrol session: motors stop NOW, the session and its
    waypoint index are kept, mode stays PATROL. Idempotent."""
    with _lock:
        if _session is None:
            return {"ok": False, "error": "no_active_patrol",
                    "paused": False}
        if not _session.get("paused"):
            _session["paused"] = True
            _session["paused_at"] = time.time()
    from services.motor_service import stop_all
    stop_all("patrol_pause")
    logger.info("Patrol paused at waypoint %d/%d",
                _session["index"], len(_session["waypoints"]))
    _log_event({"event": "paused", "route": _session["route"],
                "waypoint_index": _session["index"]})
    return {"ok": True, "paused": True, "session": _session_status()}


def resume_patrol() -> dict:
    """Resume a paused patrol session from the same waypoint. If the waypoint
    was paused a long time, its timeout clock restarts (fresh attempt)."""
    with _lock:
        if _session is None:
            return {"ok": False, "error": "no_active_patrol", "resumed": False}
        if not _session.get("paused"):
            return {"ok": True, "resumed": False, "note": "not_paused",
                    "session": _session_status()}
        _session["paused"] = False
        _session["paused_at"] = None
        # Give the waypoint a fresh timeout window after a pause.
        _session["waypoint_started_at"] = time.time()
    logger.info("Patrol resumed at waypoint %d/%d",
                _session["index"], len(_session["waypoints"]))
    _log_event({"event": "resumed", "route": _session["route"],
                "waypoint_index": _session["index"]})
    return {"ok": True, "resumed": True, "session": _session_status()}


def _session_status() -> Optional[dict]:
    with _lock:
        if _session is None:
            return None
        sess = dict(_session)
        sess["progress"] = f"{sess['index']}/{len(sess['waypoints'])}"
        sess["current_waypoint"] = (sess["waypoints"][sess["index"]]
                                    if sess["index"] < len(sess["waypoints"]) else None)
        return sess


def route_session_active() -> bool:
    """True while a ROUTE session is active (legacy loop must defer)."""
    with _lock:
        return _session is not None


def status() -> dict:
    """Status for GET /patrol/status."""
    st = get_robot_state()
    sess = _session_status()
    active = sess is not None or _legacy_mode_requested
    out = {
        "active": active,
        "mode": st.get_mode(),
        "estop_active": st.is_estop_active(),
        "session": sess,
        "legacy_wander": _legacy_mode_requested and sess is None,
        "default_route": _default_route,
        "routes_count": len(_routes),
    }
    if sess:
        # progress/current_waypoint are computed in _session_status();
        # keep status() self-consistent even for stale snapshots.
        sess.setdefault("progress", None)
        sess.setdefault("current_waypoint", None)
    return out


def start_engine() -> None:
    """Start the engine thread (idempotent). Call from lifespan."""
    global _engine_thread
    if _engine_thread is not None and _engine_thread.is_alive():
        return
    _engine_stop.clear()
    _engine_thread = threading.Thread(target=_engine_loop, name="sentra-patrol-engine", daemon=True)
    _engine_thread.start()


def stop_engine() -> None:
    _engine_stop.set()
