"""
SENTRA — Final Flutter app compatibility REST layer (Phase 21).

Implements the app's final API spec (§1-14) on top of the internal Phase 0-20
services WITHOUT touching the APIS.md contracts (/api/v1/* stay byte-identical):

    POST /api/auth/login              → JWT via core.auth (admin/user→OWNER/GUARD)
    POST /api/auth/rover/pair         → JWT kind=node, role rover
    GET  /api/auth/me                 → user object from token claims
    POST /api/auth/logout             → 200 {} (token revoked when supplied)
    GET  /api/robots                  → [robot object]
    GET  /api/robots/{id}             → robot object
    POST /api/robots/{id}/mode        → app mode names → internal set_mode
    POST /api/robots/{id}/control/{move,stop,brake,estop}
    GET|POST /api/robots/{id}/locations, PUT|DELETE .../locations/{location_id}
    POST /api/robots/{id}/navigate/go-to
    GET|POST /api/robots/{id}/patrol/routes, DELETE .../{route_id}
    POST /api/robots/{id}/patrol/{start,pause,stop}
    POST /api/robots/{id}/dock, /dock/cancel
    GET  /api/robots/{id}/camera/status
    GET  /api/alerts                  → flat array of app alert objects
    POST /api/alerts/{id}/dismiss
    GET  /api/people                  → app person objects
    POST /api/people                  → multipart register (name/notes/image)
    PUT  /api/people/{person_id}      → multipart update
    DELETE /api/people/{person_id}    → 204
    POST /api/calls/initiate          → call object + call_incoming WS push
    POST /api/calls/{id}/{accept,reject,end}

Standard envelopes: success → {"data": ...}; error → {"detail": "..."}.
"""

import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from core import auth as core_auth
from services import device_registry
from services import compat_map as cm

router = APIRouter(prefix="/api", tags=["App Compat"])
logger = logging.getLogger(__name__)

_START_TIME = time.time()  # for /api/health uptime


def _ok(data, status_code: int = 200):
    """Bare payload — the app spec's concrete examples (§2a, §3a, §12a, §13a)
    all show plain objects/arrays, not the {'data': ...} wrapper."""
    return JSONResponse(data, status_code=status_code)


# ── 2. Authentication ─────────────────────────────────────────────────────────

@router.post("/auth/login")
def auth_login(body: dict):
    """
    App spec §2a. Any username/password is accepted (single-unit appliance;
    identity = device/role per Phase 15). The username becomes the device id;
    role defaults to admin unless the app sends role:"user".
    """
    username = str((body or {}).get("username", "")).strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    # Phase 4: password enforced only when SENTRA_PASSWORD is configured.
    if not core_auth.check_password((body or {}).get("password")):
        raise HTTPException(status_code=401, detail="invalid credentials")
    app_role = "user" if str((body or {}).get("role", "admin")).lower() == "user" else "admin"
    internal_role = cm.APP_ROLE_TO_INTERNAL[app_role]
    try:
        session = core_auth.issue_session(username, internal_role)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    device_registry.upsert_device(username, kind="user")
    user = cm.user_object(username, app_role)
    return _ok({
        "token": session["token"],
        "refresh_token": session.get("refresh_token", ""),
        "user": user,
        # Dual-shape: the spec's Global Conventions wrap responses in
        # {"data": …} while the concrete §2a example is bare — serve both so
        # either client-side parse finds the token.
        "data": {"token": session["token"], "user": user},
    })


@router.post("/auth/rover/pair")
def auth_rover_pair(body: dict):
    """
    App spec §2b. Pairing code is accepted (single-trust LAN appliance);
    the mounted phone becomes a 'node'-kind device with role 'rover'.
    """
    body = body or {}
    device_id = str(body.get("device_id", "")).strip()
    robot_id = str(body.get("robot_id", "")).strip() or cm.ROBOT_ID
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")
    # Phase 4: the pairing code must match what the Pi displays.
    if not core_auth.check_pairing_code(body.get("pairing_code")):
        raise HTTPException(status_code=403, detail="invalid pairing code")
    # Register as node BEFORE issuing so the JWT carries kind=node.
    device_registry.upsert_device(device_id, kind="node", name="rover phone")
    try:
        session = core_auth.issue_session(device_id, "GUARD")  # rover may drive/estop
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    user = cm.user_object(device_id, "rover", robot_id=robot_id)
    return _ok({
        "token": session["token"],
        "refresh_token": session.get("refresh_token", ""),
        "user": user,
        "data": {"token": session["token"], "user": user},  # dual-shape, see login
    })


@router.get("/auth/me")
def auth_me(request: Request):
    """App spec §2c. Validates the token; returns the user object."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        ctx = core_auth.verify_token(token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    app_role = ("rover" if ctx.kind == "node"
                else cm.INTERNAL_ROLE_TO_APP.get(ctx.role, "user"))
    user = cm.user_object(ctx.device_id, app_role)
    return _ok({**user, "data": user})  # dual-shape, see login


@router.post("/auth/logout")
def auth_logout(request: Request):
    """App spec §2d. Always 200 {} (app clears local state regardless)."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if token:
        try:
            claims = core_auth.decode_claims(token)
            if claims.get("jti"):
                device_registry.revoke_token(claims["jti"])
        except Exception:
            pass
    return _ok({})


# ── 1. Health (discovery probe — unauthenticated) ────────────────────────────

@router.get("/health")
def health():
    """SENTRA_API_CONTRACT.md §0: first contact for BackendConfig.discover()."""
    import platform
    return _ok({
        "status": "ok",
        "robot_id": cm.ROBOT_ID,
        "unit_name": __import__("config").settings.unit_name,
        "mode": cm.app_mode(),
        "version": __import__("config").settings.api_version,
        "uptime_s": round(time.time() - _START_TIME, 1),
        "python": platform.python_version(),
    })


# Pairing tokens (Phase 1 contract): short-lived 6-digit codes the Pi
# displays; exchanged at /api/auth/rover/register for a device credential.
_pairing_tokens: dict[str, dict] = {}


@router.post("/auth/rover/register")
def auth_rover_register(body: dict):
    """Phase 1 contract: exchange a pairing token for a long-lived device
    credential. Shape mirrors /api/auth/rover/pair on success."""
    body = body or {}
    device_id = str(body.get("device_id", "")).strip()
    pairing_token = str(body.get("pairing_token", "")).strip()
    robot_id = str(body.get("robot_id", "")).strip() or cm.ROBOT_ID
    if not device_id or not pairing_token:
        raise HTTPException(status_code=400,
                            detail="device_id and pairing_token are required")
    entry = _pairing_tokens.get(pairing_token)
    if entry is None or entry["expires_at"] < time.time():
        _pairing_tokens.pop(pairing_token, None)
        raise HTTPException(status_code=404, detail="unknown or expired pairing token")
    if entry["robot_id"] != robot_id:
        raise HTTPException(status_code=403, detail="pairing token is for a different robot")
    # Phase 4: the pairing code must also match (defence in depth — the
    # 6-digit token alone is a small keyspace).
    if not core_auth.check_pairing_code(body.get("pairing_code")):
        raise HTTPException(status_code=403, detail="invalid pairing code")
    _pairing_tokens.pop(pairing_token, None)  # single use
    device_registry.upsert_device(device_id, kind="node", name="rover phone")
    try:
        session = core_auth.issue_session(device_id, "GUARD")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    user = cm.user_object(device_id, "rover", robot_id=robot_id)
    return _ok({
        "token": session["token"],
        "refresh_token": session.get("refresh_token", ""),
        "user": user,
        "data": {"token": session["token"], "user": user},  # dual-shape, see login
    })


@router.get("/robots/{robot_id}/pairing-token")
def get_pairing_token(robot_id: str):
    """Phase 1 contract: the 6-digit code currently displayed on the Pi
    (5-minute TTL; a fresh one is issued when expired)."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    now = time.time()
    token = next((t for t, e in _pairing_tokens.items()
                  if e["robot_id"] == robot_id and e["expires_at"] > now), None)
    if token is None:
        token = f"{secrets.randbelow(1000000):06d}"
        _pairing_tokens[token] = {"robot_id": robot_id, "expires_at": now + 300.0}
    return _ok({"token": token, "expires_at": _pairing_tokens[token]["expires_at"],
                "robot_id": robot_id})


# ── 3. Robots ─────────────────────────────────────────────────────────────────

@router.get("/robots")
def list_robots():
    return _ok([cm.robot_object()])


@router.get("/robots/{robot_id}")
def get_robot(robot_id: str):
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    return _ok(cm.robot_object())


@router.get("/robots/{robot_id}/mode")
def get_mode(robot_id: str):
    """App reads the current mode (Flutter impl probes GET before PATCH)."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    return _ok({**cm.robot_object(), "mode": cm.app_mode()})


@router.api_route("/robots/{robot_id}/mode", methods=["POST", "PATCH", "PUT"])
def change_mode(robot_id: str, body: dict):
    """App spec §3c: idle/manual/mapping/patrol/auto/docking → internal modes.
    PATCH/PUT accepted too — the Flutter impl uses PATCH."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    app_mode = str((body or {}).get("mode", "")).strip().lower()
    from services import motor_service, patrol_service, docking_service, navigation_service
    app_mode = str((body or {}).get("mode", "")).strip().lower()
    if app_mode not in cm.APP_TO_MODE:
        raise HTTPException(status_code=400,
                            detail="mode must be one of idle|manual|mapping|patrol|auto|docking")
    internal = cm.APP_TO_MODE[app_mode]
    # Leaving autonomy: end any running session for a clean handover.
    if internal in ("STANDBY", "MANUAL"):
        try:
            patrol_service.stop_patrol()
            docking_service.cancel_return("mode_change")
            navigation_service.cancel("mode_change")
        except Exception:
            pass
    if internal == "PATROL":
        result = patrol_service.start_patrol()
        if not result.get("ok"):
            raise HTTPException(status_code=409,
                                 detail=result.get("error") or "patrol refused")
    elif internal == "RETURN_TO_DOCK":
        result = docking_service.start_return(docked_by="app_mode")
        if not result.get("ok"):
            raise HTTPException(status_code=409,
                                 detail=result.get("error") or "docking refused")
    else:
        result = motor_service.set_mode(internal)
        if not result.get("ok", result.get("accepted", True)):
            raise HTTPException(status_code=409,
                                 detail=result.get("error") or "mode refused")
    return _ok({**cm.robot_object(), "mode": app_mode})


# ── 5. Manual control (REST fallback / secondary) ────────────────────────────

def _motion():
    from services.motion_controller import get_motion_controller
    return get_motion_controller()


@router.post("/robots/{robot_id}/control/move")
def control_move(robot_id: str, body: dict):
    """App spec §5: left/right in -1..1 (differential wheel speeds)."""
    body = body or {}
    try:
        left = float(body.get("left_speed", 0.0)) * 100.0
        right = float(body.get("right_speed", 0.0)) * 100.0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="left_speed/right_speed must be numbers")
    decision = _motion().set_wheel_target(left, right, source="app_compat", stream=True)
    if not decision.get("applied"):
        raise HTTPException(status_code=409, detail=decision.get("error", "refused"))
    return _ok({"applied": True})


@router.post("/robots/{robot_id}/control/stop")
def control_stop(robot_id: str):
    _motion().stop("app_compat")
    return _ok({"applied": True})


@router.post("/robots/{robot_id}/control/brake")
def control_brake(robot_id: str):
    _motion().brake("app_compat")
    return _ok({"applied": True})


@router.post("/robots/{robot_id}/control/estop")
def control_estop(robot_id: str):
    """App spec §5 REST e-stop fallback — same path as the app's WS estop."""
    from services import motor_service
    result = motor_service.trigger_estop()
    logger.critical("E-STOP triggered via app compat REST")
    return _ok(result)


@router.get("/robots/{robot_id}/sensors")
def robot_sensors(robot_id: str):
    """Phase 1 contract: REST mirror of the sensor_update WS event
    (used by the dashboard when the socket is reconnecting)."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    return _ok(cm.sensor_event())


@router.get("/robots/{robot_id}/status")
def robot_status(robot_id: str):
    """Phase 1 contract: everything the dashboard needs in one GET when the
    WS is down — robot object + navigation status + telemetry block."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    return _ok({
        **cm.robot_object(),
        "mode": cm.app_mode(),
        "navigation": cm.navigation_event(),
        "telemetry": cm.telemetry_event(),
    })


# ── 7. Locations (tag map) ────────────────────────────────────────────────────

@router.get("/robots/{robot_id}/locations")
def list_locations(robot_id: str):
    from services import tag_map
    return _ok([cm.location_object(t) for t in tag_map.list_tags()])


@router.post("/robots/{robot_id}/locations")
def create_location(robot_id: str, body: dict):
    from services import tag_map
    body = body or {}
    try:
        tag = tag_map.upsert_tag(int(body.get("tag_id")), str(body.get("name", "")),
                                 type="LOCATION",
                                 notes=str(body.get("description", "") or ""))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(cm.location_object(tag), status_code=201)


@router.api_route("/robots/{robot_id}/locations/{location_id}",
                  methods=["PUT", "PATCH"])  # Phase 1: Flutter PATCHes this
def update_location(robot_id: str, location_id: str, body: dict):
    from services import tag_map
    if not location_id.startswith("loc-"):
        raise HTTPException(status_code=400, detail="location_id must be loc-<tag_id>")
    try:
        tag_id = int(location_id[4:])
        tag = tag_map.rename_tag(tag_id,
                                 name=(body or {}).get("name"),
                                 notes=(body or {}).get("description"))
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown location")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(cm.location_object(tag))


@router.delete("/robots/{robot_id}/locations/{location_id}")
def delete_location(robot_id: str, location_id: str):
    from services import tag_map
    if not location_id.startswith("loc-"):
        raise HTTPException(status_code=400, detail="location_id must be loc-<tag_id>")
    try:
        tag_id = int(location_id[4:])
    except ValueError:
        raise HTTPException(status_code=400, detail="location_id must be loc-<tag_id>")
    if not tag_map.delete_tag(tag_id):
        raise HTTPException(status_code=404, detail="unknown location")
    return Response(status_code=204)


@router.post("/robots/{robot_id}/navigate/go-to")
def navigate_go_to(robot_id: str, body: dict):
    """App spec §7e. Resolves location_id (or a raw name) → navigation session."""
    from services import tag_map, navigation_service
    body = body or {}
    loc = str(body.get("location_id", "")).strip()
    tag = None
    if loc.startswith("loc-"):
        try:
            tag = tag_map.get_tag(int(loc[4:]))
        except ValueError:
            tag = None
    if tag is None:
        tag = tag_map.find_by_name(loc)
    if tag is None:
        raise HTTPException(status_code=404, detail="unknown location")
    if tag.get("type") == "DOCK":
        from services import docking_service
        result = docking_service.start_return(docked_by="app_goto")
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "refused"))
        return _ok({"started": True, "target": tag["name"], "docking": True})
    result = navigation_service.go_to(tag["tag_id"], tag["name"], source="app_compat")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"started": True, "target": tag["name"]})


@router.post("/robots/{robot_id}/standby")
def robot_standby(robot_id: str):
    """Phase 1 contract (app spec §6e): the Pi performs the return-to-dock
    navigation itself and stands by."""
    if robot_id != cm.ROBOT_ID:
        raise HTTPException(status_code=404, detail="unknown robot_id")
    from services import docking_service
    result = docking_service.start_return(docked_by="app_standby")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"started": True, "mode": "docking"})


# ── 8. Patrol routes ──────────────────────────────────────────────────────────

@router.get("/robots/{robot_id}/patrol/routes")
def list_routes(robot_id: str):
    from services import patrol_service
    routes = patrol_service.list_routes()
    return _ok([cm.route_object(name, wps)
                for name, wps in sorted(routes.items())])


@router.post("/robots/{robot_id}/patrol/routes")
def create_route(robot_id: str, body: dict):
    from services import patrol_service, tag_map
    body = body or {}
    raw_wps = body.get("waypoints") or []
    if not isinstance(raw_wps, list):
        raise HTTPException(status_code=400, detail="waypoints must be a list")
    names = []
    for wp in raw_wps:
        tag = None
        if str(wp).startswith("loc-"):
            try:
                tag = tag_map.get_tag(int(str(wp)[4:]))
            except ValueError:
                tag = None
        tag = tag or tag_map.find_by_name(str(wp))
        if tag is None:
            raise HTTPException(status_code=400, detail=f"unknown waypoint: {wp}")
        names.append(tag["name"])
    if not body.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    result = patrol_service.save_route(str(body["name"]), names)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "refused"))
    return _ok(cm.route_object(str(body["name"]), names), status_code=201)


@router.delete("/robots/{robot_id}/patrol/routes/{route_id}")
def delete_route(robot_id: str, route_id: str):
    from services import patrol_service
    if not route_id.startswith("route-"):
        raise HTTPException(status_code=400, detail="route_id must be route-<name>")
    result = patrol_service.delete_route(route_id[6:])
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "unknown route"))
    return _ok({"deleted": True})


@router.post("/robots/{robot_id}/patrol/start")
def patrol_start(robot_id: str, body: dict):
    from services import patrol_service
    route_name = None
    rid = str(((body or {}).get("route_id") or "")).strip()
    if rid.startswith("route-"):
        route_name = rid[6:]
    elif rid:
        route_name = rid
    result = patrol_service.start_patrol(route_name or None)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"started": True, "session": result.get("session")})


@router.post("/robots/{robot_id}/patrol/pause")
def patrol_pause(robot_id: str):
    """Phase 5 (P10): real pause — the session and waypoint index are kept,
    motors stop, mode stays patrol."""
    from services import patrol_service
    result = patrol_service.pause_patrol()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"paused": True, "session": result.get("session")})


@router.post("/robots/{robot_id}/patrol/resume")
def patrol_resume(robot_id: str):
    """Phase 5 (P10): resume from the paused waypoint (was: fresh start)."""
    from services import patrol_service
    result = patrol_service.resume_patrol()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"resumed": True, "session": result.get("session")})


@router.post("/robots/{robot_id}/patrol/stop")
def patrol_stop(robot_id: str):
    from services import patrol_service
    result = patrol_service.stop_patrol()
    return _ok({"stopped": bool(result.get("ok"))})


# ── 9. Return to dock ────────────────────────────────────────────────────────

@router.post("/robots/{robot_id}/dock")
def dock_start(robot_id: str):
    from services import docking_service
    result = docking_service.start_return(docked_by="app_compat")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "refused"))
    return _ok({"started": True})


@router.post("/robots/{robot_id}/dock/cancel")
def dock_cancel(robot_id: str):
    from services import docking_service
    return _ok(docking_service.cancel_return("app_compat"))


# ── 11. Camera ────────────────────────────────────────────────────────────────

@router.get("/robots/{robot_id}/camera/status")
def camera_status(robot_id: str):
    return _ok(cm.camera_status())


# ── 12. Alerts ────────────────────────────────────────────────────────────────

@router.get("/alerts")
def list_alerts():
    from services import notification_service
    items = notification_service.get(severity="ALL", limit=500)
    return JSONResponse([cm.alert_object(n) for n in items])


def _get_notification(alert_id: str):
    from services import notification_service
    for n in notification_service.get(severity="ALL", limit=500):
        if n["id"] == alert_id:
            return n
    raise HTTPException(status_code=404, detail="unknown alert")


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str):
    """Phase 1 contract: alert detail for the alert-detail screen."""
    return _ok(cm.alert_object(_get_notification(alert_id)))


@router.get("/alerts/{alert_id}/image")
def get_alert_image(alert_id: str):
    """Phase 3: serve the JPEG snapshot attached to the alert (unknown-person
    events carry one from person_recognition). Falls back to 404 when the
    event had no snapshot or the file is gone."""
    import os
    from fastapi.responses import Response as _Response
    from services import person_recognition
    n = _get_notification(alert_id)
    image_file = n.get("image_file")
    if not image_file:
        raise HTTPException(status_code=404, detail="no image attached")
    path = os.path.join(person_recognition.SNAPSHOT_DIR,
                        os.path.basename(image_file))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="snapshot file missing")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="snapshot file missing")
    return _Response(content=data, media_type="image/jpeg")


@router.api_route("/alerts/{alert_id}/dismiss", methods=["POST", "PATCH"])
def dismiss_alert(alert_id: str):
    """App spec uses PATCH; the original Phase 21 build used POST — both work."""
    from services import notification_service
    ok = notification_service.ack(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown alert")
    return _ok({})


# ── 13. People registry ──────────────────────────────────────────────────────

@router.get("/people")
def list_people():
    from services import person_registry, compat_map as _cm
    return JSONResponse([_cm.person_object(p) for p in person_registry.list_persons()])


async def _read_multipart(request: Request) -> tuple[bytes, str | None, str | None]:
    """Return (image_bytes, name, notes) from a multipart form (stdlib email
    parser — field order and boundary quirks tolerated)."""
    """Return (image_bytes, name, notes) from a multipart form."""
    import email
    import email.policy
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    if "multipart/form-data" not in ctype:
        return body or b"", None, None
    raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
    msg = email.message_from_bytes(raw, policy=email.policy.HTTP)
    image = b""
    name = notes = None
    for part in msg.iter_parts():
        field = part.get_param("name", header="content-disposition")
        if field == "image":
            image = part.get_payload(decode=True) or b""
        elif field == "name":
            name = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip()
        elif field == "notes":
            notes = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip()
    return image, name, notes


@router.post("/people")
async def register_person_compat(request: Request):
    """App spec §13b: multipart name/image/notes → register + embed."""
    from services import person_recognition, person_registry
    image, name, notes = await _read_multipart(request)
    if not name:
        raise HTTPException(status_code=400, detail="name field is required")
    import cv2
    import numpy as np
    frame = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR) if image else None
    if frame is None:
        raise HTTPException(status_code=400, detail="image field must be a JPEG/PNG photo")
    faces = person_recognition.detect_faces(frame)
    if not faces:
        raise HTTPException(status_code=400, detail="no face found in photo")
    embeddings = []
    for (x, y, w, h) in faces:
        crop = frame[max(0, y):y + h, max(0, x):x + w]
        emb = person_recognition.embed_face(crop)
        if emb:
            embeddings.append(emb)
    if not embeddings:
        raise HTTPException(status_code=400, detail="could not compute embeddings")
    snapshot_path = person_recognition._save_unknown_snapshot(frame)
    try:
        record = person_registry.add_person(name, embeddings,
                                            snapshot_path=snapshot_path,
                                            notes=notes or "")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _ok(cm.person_object(record), status_code=201)


@router.put("/people/{person_id}")
async def update_person_compat(person_id: str, request: Request):
    """App spec §13c: multipart partial update (name/notes/image)."""
    from services import person_registry, person_recognition
    image, name, notes = await _read_multipart(request)
    snapshot_path = None
    if image:
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is not None:
            snapshot_path = person_recognition._save_unknown_snapshot(frame)
    try:
        record = person_registry.update_person(person_id, name=name,
                                               notes=notes,
                                               snapshot_path=snapshot_path)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown person")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(cm.person_object(record))


@router.delete("/people/{person_id}")
def delete_person_compat(person_id: str):
    from services import person_registry
    if not person_registry.delete_person(person_id):
        raise HTTPException(status_code=404, detail="unknown person")
    return Response(status_code=204)


# ── 14. Video calling ─────────────────────────────────────────────────────────

_calls: dict[str, dict] = {}


def register_call(call: dict) -> None:
    """Store a call + push call_incoming. Used by REST initiate AND the WS
    CALL_USER voice path (ws_handlers/app_ws.py)."""
    _calls[call["id"]] = call
    from ws_handlers import compat_ws
    compat_ws.push_event("call_incoming",
                         {"callId": call["id"], "robotId": call["robot_id"]})


def _call_object(call: dict) -> dict:
    return {
        "id": call["id"],
        "robot_id": call["robot_id"],
        "status": call["status"],
        "started_at": cm.iso(call["started_at"]),
    }


@router.post("/calls/initiate")
def call_initiate(body: dict):
    """App spec §14a: create a ringing call + push call_incoming on /ws."""
    import uuid
    from ws_handlers import compat_ws
    body = body or {}
    robot_id = str(body.get("robot_id", "")).strip() or cm.ROBOT_ID
    call = {
        "id": f"call-{uuid.uuid4().hex[:8]}",
        "robot_id": robot_id,
        "status": "ringing",
        "started_at": time.time(),
    }
    _calls[call["id"]] = call
    compat_ws.push_event("call_incoming",
                         {"callId": call["id"], "robotId": robot_id})
    # Bridge into the emergency-call lifecycle when a session exists (fall).
    try:
        from services import emergency_call
        sess = emergency_call.get_status().get("session")
        if sess:
            emergency_call.note_presence("user", True)
    except Exception:
        pass
    return _ok(_call_object(call))


def _get_call(call_id: str) -> dict:
    call = _calls.get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="unknown call")
    return call


@router.get("/calls/{call_id}")
def get_call(call_id: str):
    """Phase 1 contract: call polling for the call screen."""
    return _ok(_call_object(_get_call(call_id)))


@router.get("/calls/{call_id}")
def get_call(call_id: str):
    """Phase 1 contract: call polling for the call screen."""
    return _ok(_call_object(_get_call(call_id)))


@router.post("/calls/{call_id}/accept")
def call_accept(call_id: str):
    call = _get_call(call_id)
    call["status"] = "active"
    return _ok(_call_object(call))


@router.post("/calls/{call_id}/reject")
def call_reject(call_id: str):
    call = _get_call(call_id)
    call["status"] = "missed"
    return _ok(_call_object(call))


@router.post("/calls/{call_id}/end")
def call_end(call_id: str):
    from ws_handlers import compat_ws
    call = _get_call(call_id)
    call["status"] = "ended"
    compat_ws.push_event("call_ended", {"callId": call["id"]})
    return _ok(_call_object(call))
