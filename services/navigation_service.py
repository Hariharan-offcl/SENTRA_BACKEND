"""
SENTRA — Navigation service (Phase 9).

Generic "go to location X" engine — the docking state machine generalized to
any mapped tag (docking stays separate with its tighter alignment semantics).

NAVIGATION mode (owner "navigation_service"):
    SEEK      rotate until the target tag is visible (timeout → abort)
    APPROACH  bearing-steered drive; slow near; ARRIVED when tag distance
              ≤ NAV_ARRIVE_M; tag lost > DOCK_TAG_LOST_GRACE_S → SEEK
    ARRIVED   stop → STANDBY, NAVIGATION event pushed to /ws/alerts

Voice ("SENTRA KITCHEN") → voice_service resolves "kitchen" via the tag map →
navigation_service.go_to(tag_id) drives this engine. Safety gate active
throughout; manual takeover / e-stop / mode change cancels.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from core import config as core_config
from core.state import get_robot_state, NAVIGATION, STANDBY
from services import safety_events

logger = logging.getLogger(__name__)

ENGINE_TICK_S = 0.1
BLOCK_RETRY_WINDOW_S = 3.0

_lock = threading.RLock()
_session: Optional[dict] = None
_engine_thread: threading.Thread | None = None
_engine_stop = threading.Event()


def _log_event(detail: dict, severity: str = "INFO") -> None:
    try:
        safety_events.report("NAVIGATION", detail, severity=severity)
    except Exception:
        pass


def _latest_detection(tag_id: int, max_age_s: float) -> Optional[dict]:
    from services import apriltag_service
    det = apriltag_service.get_last_seen(tag_id)
    if det and (time.time() - det["timestamp"]) <= max_age_s:
        return det
    return None


# ── Session API ──────────────────────────────────────────────────────────────

def _plan_hops(target_name: str, tag_id: int) -> tuple[list[dict], Optional[str]]:
    """Phase 5 (P11): plan the hop list through the taught route graph.
    Returns (hops, current_location_name). Falls back to a single direct hop
    whenever the graph can't help (no current fix, no path, stale edges) —
    a sparse graph must never make go_to refuse."""
    from services import tag_map, route_graph, localization_service
    direct = [{"name": target_name, "tag_id": int(tag_id)}]
    try:
        loc = localization_service.get_localization().get("last_known") or {}
        current = loc.get("name")
    except Exception:
        current = None
    if not current or current == target_name:
        return direct, current
    path = route_graph.find_path(current, target_name)
    if not path or len(path) < 2:
        return direct, current
    hops = []
    for name in path[1:]:
        t = tag_map.find_by_name(name)
        if t is None:
            return direct, current  # graph edge references a deleted tag
        hops.append({"name": name, "tag_id": int(t["tag_id"])})
    if not hops:
        return direct, current
    return hops, current


def go_to(tag_id: int, target_name: str, source: str = "voice") -> dict:
    """Start navigating to the location bound to tag_id. When the taught
    route graph knows a path from the current location, the session walks it
    hop by hop (P11); otherwise it is the original direct per-tag nav."""
    global _session
    st = get_robot_state()
    if st.is_estop_active():
        return {"ok": False, "error": "estop_latched"}
    result = st.request_mode(NAVIGATION, requested_by="navigation_service")
    if not result.get("accepted"):
        return {"ok": False, "error": f"mode_unavailable ({st.get_mode()})"}

    hops, prev_node = _plan_hops(target_name, tag_id)
    with _lock:
        _session = {
            "id": f"nav-{int(time.time())}",
            "state": "SEEK",
            "tag_id": int(hops[0]["tag_id"]),
            "target": hops[0]["name"],
            "final_target": target_name,
            "source": source,
            "started_at": time.time(),
            "state_started_at": time.time(),
            "blocked_since": None,
            "search_direction": 1,
            "hops": hops,
            "hop_index": 0,
            "prev_node": prev_node,
            "completed_hops": [],
        }
    hop_desc = " → ".join(h["name"] for h in hops)
    logger.info("Navigation %s: go to '%s' via %s%s",
                _session["id"], target_name, source,
                f" | hops: {hop_desc}" if len(hops) > 1 else "")
    _log_event({"event": "started", "target": target_name, "tag_id": tag_id,
                "source": source,
                "hops": [h["name"] for h in hops] if len(hops) > 1 else None})
    return {"ok": True, "session": status()}


def cancel(reason: str = "cancelled") -> dict:
    global _session
    with _lock:
        had = _session is not None
        if _session:
            _session["ended_reason"] = reason
            _session = None
    from services.motor_service import stop_all
    stop_all("nav_cancel")
    st = get_robot_state()
    if st.get_mode() == NAVIGATION:
        st.request_mode(STANDBY, requested_by="navigation_service")
    if had:
        logger.info("Navigation cancelled (%s)", reason)
        _log_event({"event": "cancelled", "reason": reason})
    return {"ok": True, "was_active": had}


def status() -> Optional[dict]:
    with _lock:
        if _session is None:
            return None
        sess = dict(_session)
        sess["age_s"] = round(time.time() - sess["started_at"], 1)
        sess["hop_index"] = sess.get("hop_index", 0)
        sess["hop_count"] = len(sess.get("hops") or [])
        sess["final_target"] = sess.get("final_target") or sess["target"]
        return sess


# ── State handlers (mirror docking semantics, looser arrival) ───────────────

def _set_state(sess: dict, new_state: str) -> None:
    with _lock:
        if _session and _session["id"] == sess["id"]:
            _session["state"] = new_state
            _session["state_started_at"] = time.time()
            _session["blocked_since"] = None
    logger.info("Nav %s: %s → %s", sess["id"], sess["state"], new_state)


def _handle_gate_refusal(sess: dict) -> None:
    from services.motor_service import stop_all
    stop_all("nav_gate")
    now = time.time()
    with _lock:
        if _session and _session["id"] == sess["id"]:
            if _session["blocked_since"] is None:
                _session["blocked_since"] = now
            blocked_for = now - _session["blocked_since"]
        else:
            blocked_for = 0.0
    if blocked_for > BLOCK_RETRY_WINDOW_S and sess["state"] == "APPROACH":
        _set_state(sess, "SEEK")


def _state_seek(sess: dict) -> None:
    from services.motor_service import apply_wheel_speeds
    if time.time() - sess["state_started_at"] > core_config.NAV_SEARCH_TIMEOUT_S:
        logger.warning("Nav %s: search timed out", sess["id"])
        _log_event({"event": "search_timeout", "target": sess["target"]}, severity="WARNING")
        cancel("search_timeout")
        return
    det = _latest_detection(sess["tag_id"], max_age_s=core_config.PATROL_CONFIRM_FRESH_S)
    if det is not None:
        _set_state(sess, "APPROACH")
        return
    direction = 1 if sess.get("search_direction", 1) > 0 else -1
    duty = core_config.DOCK_SEARCH_TURN_DUTY
    decision = apply_wheel_speeds(direction * duty, -direction * duty,
                                  owner="navigation_service", mode=NAVIGATION)
    if not decision.get("applied"):
        _handle_gate_refusal(sess)


def _state_approach(sess: dict) -> None:
    from services.motor_service import apply_wheel_speeds
    if time.time() - sess["state_started_at"] > core_config.NAV_APPROACH_TIMEOUT_S:
        logger.warning("Nav %s: approach timed out", sess["id"])
        _log_event({"event": "approach_timeout", "target": sess["target"]}, severity="WARNING")
        cancel("approach_timeout")
        return
    det = _latest_detection(sess["tag_id"], max_age_s=core_config.DOCK_TAG_LOST_GRACE_S)
    if det is None:
        logger.warning("Nav %s: tag lost — re-seeking", sess["id"])
        _set_state(sess, "SEEK")
        return

    distance = det.get("distance_m")
    bearing = det.get("bearing_deg") or 0.0
    if distance is not None and distance <= core_config.NAV_ARRIVE_M:
        _arrived(sess)
        return

    duty = (core_config.DOCK_NEAR_DUTY if distance is not None
            and distance < core_config.DOCK_SLOW_M else core_config.DOCK_APPROACH_DUTY)
    steer = max(-40.0, min(40.0, bearing * core_config.DOCK_STEER_GAIN))
    decision = apply_wheel_speeds(duty - steer, duty + steer,
                                  owner="navigation_service", mode=NAVIGATION)
    if not decision.get("applied"):
        _handle_gate_refusal(sess)
    else:
        with _lock:
            if _session and _session["id"] == sess["id"]:
                _session["blocked_since"] = None


def _arrived(sess: dict) -> None:
    """Phase 5 (P11): arrival at the CURRENT hop. Intermediate hops advance
    the session to the next hop (teaching the driven edge into the graph);
    the final hop ends the session like the old single-hop arrival."""
    global _session
    with _lock:
        if _session is None or _session["id"] != sess["id"]:
            return
        hops = _session.get("hops") or []
        i = _session.get("hop_index", 0)
        hop = hops[i] if i < len(hops) else {
            "name": _session["target"], "tag_id": _session["tag_id"]}
        _session["completed_hops"].append(hop["name"])
        completed = _session["completed_hops"]
        prev_node = (_session.get("prev_node")
                     if len(completed) == 1 else completed[-2])
        next_i = i + 1
        advance = next_i < len(hops)
        if advance:
            nxt = hops[next_i]
            _session["hop_index"] = next_i
            _session["tag_id"] = nxt["tag_id"]
            _session["target"] = nxt["name"]
            _session["state"] = "SEEK"
            _session["state_started_at"] = time.time()
            _session["blocked_since"] = None
            _session["prev_node"] = hop["name"]
        else:
            final = _session.get("final_target") or hop["name"]
            _session = None
    # Teach the edge we just drove (auto-learning, never raises).
    from services import route_graph
    route_graph.observe_traversal(prev_node, hop["name"])

    from services.motor_service import stop_all
    stop_all("nav_done")
    if advance:
        logger.info("Nav: hop arrived at '%s' → next hop '%s'",
                    hop["name"], nxt["name"])
        _log_event({"event": "hop", "arrived": hop["name"],
                    "next": nxt["name"]})
        return
    st = get_robot_state()
    if st.get_mode() == NAVIGATION:
        st.request_mode(STANDBY, requested_by="navigation_service")
    logger.info("Nav: ARRIVED at '%s' (tag %s)", final, hop["tag_id"])
    _log_event({"event": "arrived", "target": final, "tag_id": hop["tag_id"]})


# ── Engine ───────────────────────────────────────────────────────────────────

def _engine_loop() -> None:
    handlers = {"SEEK": _state_seek, "APPROACH": _state_approach}
    logger.info("Navigation engine started (tick=%.2fs)", ENGINE_TICK_S)
    while not _engine_stop.is_set():
        sess = status()
        if sess is None:
            _engine_stop.wait(ENGINE_TICK_S)
            continue
        st = get_robot_state()
        if st.is_estop_active() or st.get_mode() != NAVIGATION:
            reason = "estop" if st.is_estop_active() else f"mode_changed_to_{st.get_mode()}"
            cancel(reason)
            continue
        handler = handlers.get(sess["state"])
        if handler:
            try:
                handler(sess)
            except Exception as exc:
                logger.error("Nav %s error: %s", sess["id"], exc)
                cancel("engine_error")
        _engine_stop.wait(ENGINE_TICK_S)
    logger.info("Navigation engine stopped")


def start_engine() -> None:
    global _engine_thread
    if _engine_thread is not None and _engine_thread.is_alive():
        return
    _engine_stop.clear()
    _engine_thread = threading.Thread(target=_engine_loop, name="sentra-nav", daemon=True)
    _engine_thread.start()


def stop_engine() -> None:
    _engine_stop.set()
