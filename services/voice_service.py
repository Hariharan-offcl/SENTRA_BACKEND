"""
SENTRA — Voice command service (Phase 9).

The Flutter app converts speech to text ("SENTRA KITCHEN") and posts a
STRUCTURED command here — the Pi never runs speech recognition.

    POST /api/v1/voice/command
        {"command": "go_to",       "target": "kitchen"}
        {"command": "start_patrol"}
        {"command": "stop"}
        {"command": "go_to_dock"}
        {"command": "call_user",   "target": "family"}
        {"raw": "sentra kitchen"}            ← convenience free-text parse

Target resolution: case-insensitive, whitespace/underscore-insensitive and
substring-tolerant lookup through the tag map ("kitchen" → tag 2, "kit" also
matches if unambiguous). go_to dispatches the Phase 9 navigation engine.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.state import get_robot_state
from services import tag_map

logger = logging.getLogger(__name__)

COMMANDS = ("go_to", "go_to_dock", "start_patrol", "stop", "call_user")

# Free-text keywords → structured commands (matched after 'sentra' wake word)
_KEYWORDS = [
    ("go to dock", "go_to_dock"),
    ("go dock", "go_to_dock"),
    ("dock", "go_to_dock"),
    ("start patrol", "start_patrol"),
    ("patrol", "start_patrol"),
    ("stop", "stop"),
    ("call", "call_user"),
]
# Keywords that must be tried BEFORE 'go_to' prefix matching
_GO_TO_PATTERNS = ("go to ", "goto ", "go ")


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().strip().split())


def resolve_target(target: str) -> Optional[dict]:
    """Fuzzy-resolve a spoken location name through the tag map.
    Exact → prefix → substring; returns the tag entry or None."""
    if not target:
        return None
    needle = _normalize(target).replace("_", " ")
    if not needle:
        return None
    tags = tag_map.list_tags()
    # 1) exact
    for t in tags:
        if _normalize(t["name"]) == needle:
            return t
    # 2) prefix
    for t in tags:
        if _normalize(t["name"]).startswith(needle):
            return t
    # 3) substring — space-insensitive both ways ("bed room" → "Bedroom")
    needle_nosp = needle.replace(" ", "")
    for t in tags:
        name_nosp = _normalize(t["name"]).replace(" ", "")
        if needle_nosp in name_nosp or name_nosp in needle_nosp:
            return t
    return None


def parse_raw(text: str) -> Optional[dict]:
    """Best-effort parse of a free-text utterance into a structured command.
    Wake words are removed anywhere in the utterance; keyword patterns are
    matched with a soft word boundary (not only at the start)."""
    t = _normalize(text)
    # strip wake word variants anywhere
    for wake in ("sentra", "center", "centra"):
        t = t.replace(wake, " ")
    t = _normalize(t)
    if not t:
        return None
    padded = f" {t} "
    for pattern, command in _KEYWORDS:
        if f" {pattern}" in padded:
            remainder = padded.split(pattern, 1)[1].strip()
            if command == "go_to_dock":
                return {"command": command}
            if command == "call_user":
                return {"command": command, "target": remainder or None}
            return {"command": command}
    for pattern in _GO_TO_PATTERNS:
        if f" {pattern}" in padded:
            target = padded.split(pattern, 1)[1].strip()
            if target:
                return {"command": "go_to", "target": target}
    # Bare location name? ("sentra kitchen")
    if resolve_target(t):
        return {"command": "go_to", "target": t}
    return None


# ── Dispatchers ──────────────────────────────────────────────────────────────

def _dispatch_go_to(target: str) -> dict:
    from services import navigation_service
    tag = resolve_target(target)
    if tag is None:
        known = [t["name"] for t in tag_map.list_tags()]
        return {"handled": False, "error": f"unknown_location:{target}",
                "known_locations": known}
    result = navigation_service.go_to(tag["tag_id"], tag["name"], source="voice")
    if not result.get("ok"):
        return {"handled": False, "error": result.get("error")}
    return {"handled": True, "action": "go_to", "target": tag["name"],
            "tag_id": tag["tag_id"], "state": result["session"]["state"]}


def _dispatch_go_to_dock() -> dict:
    dock = next((t for t in tag_map.list_tags() if t.get("type") == "DOCK"), None)
    if dock is None:
        return {"handled": False, "error": "no_dock_tag_registered"}
    return _dispatch_go_to(dock["name"])


def _dispatch_start_patrol() -> dict:
    from services import patrol_service
    result = patrol_service.start_patrol()
    if not result.get("ok"):
        return {"handled": False, "error": result.get("error", "patrol_unavailable")}
    out = {"handled": True, "action": "start_patrol"}
    if result.get("session"):
        out["route"] = result["session"].get("route")
        out["state"] = "PATROL"
    else:
        out["mode"] = "legacy_wander"
    return out


def _dispatch_stop() -> dict:
    from services import patrol_service, docking_service, navigation_service
    patrol_service.stop_patrol()
    docking_service.cancel_return("voice_stop")
    navigation_service.cancel("voice_stop")
    from services.motor_service import stop_all
    stop_all("voice_stop")
    st = get_robot_state()
    if st.get_mode() != "EMERGENCY_STOP":
        st.request_mode("STANDBY", requested_by="voice_service")
    return {"handled": True, "action": "stop", "state": "STANDBY"}


def _dispatch_call_user(target: Optional[str]) -> dict:
    # Phase 13 wires the real call workflow; acknowledge cleanly today.
    logger.info("Voice call_user target=%r acknowledged (call phase pending)", target)
    return {"handled": True, "action": "call_user", "target": target,
            "note": "call workflow arrives in Phase 13"}


# ── Entry point ──────────────────────────────────────────────────────────────

def execute(command: str, target: Optional[str] = None,
            raw: Optional[str] = None) -> dict:
    """Execute a structured voice command. Never raises."""
    started = time.time()
    try:
        # Convenience: raw text → structured
        if not command and raw:
            parsed = parse_raw(raw)
            if parsed is None:
                return {"handled": False, "error": "unrecognized",
                        "commands": list(COMMANDS)}
            command = parsed.get("command", "")
            target = target or parsed.get("target")

        command = _normalize(command).replace(" ", "_")
        if command not in COMMANDS:
            return {"handled": False, "error": f"unknown_command:{command}",
                    "commands": list(COMMANDS)}

        if command == "go_to":
            if not target:
                return {"handled": False, "error": "missing_target",
                        "known_locations": [t["name"] for t in tag_map.list_tags()]}
            result = _dispatch_go_to(target)
        elif command == "go_to_dock":
            result = _dispatch_go_to_dock()
        elif command == "start_patrol":
            result = _dispatch_start_patrol()
        elif command == "stop":
            result = _dispatch_stop()
        elif command == "call_user":
            result = _dispatch_call_user(target)
        else:  # pragma: no cover
            result = {"handled": False, "error": "unreachable"}

        result["command"] = command
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result
    except Exception as exc:
        logger.error("Voice command failed: %s", exc)
        return {"handled": False, "error": f"internal:{exc}"}
