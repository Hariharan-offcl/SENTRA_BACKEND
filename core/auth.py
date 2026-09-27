"""
SENTRA — Auth core (Phase 15).

JWT issue/verify (python-jose, already the project dependency) + the
FastAPI dependency used to enforce auth on mutating endpoints, and the
WebSocket gate used by the control socket.

Design:
    - POST /api/v1/auth/session stays OPEN (it is the login endpoint).
    - Every session token carries claims {sub, role, permissions, kind,
      jti, iat, exp}; the jti is tracked in the device registry so tokens
      are revocable (logout) and idle-expire (sliding 24 h window).
    - Refresh: POST /auth/refresh with a refresh token → new pair; the old
      access token's jti is revoked (rotation).
    - ENFORCEMENT this phase (staged rollout):
        * all mutating REST endpoints (POST/PUT/DELETE) except the open
          list below
        * /ws/control (motor-driving socket)
      Read-only REST and telemetry/alerts/call WS stay open for the Flutter
      app until it ships token plumbing (contract preserved).
    - SENTRA_AUTH_ENFORCED=false turns enforcement off (dev/emulator).

Open (unauthenticated) REST paths, even when enforcement is on:
    /api/v1/ping, /api/v1/auth/session, /api/v1/auth/refresh,
    /api/v1/pair, /api/v1/docs, /api/v1/openapi.json (+ /docs, /openapi.json)
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, WebSocket, status
from jose import JWTError, jwt

from config import settings
from services import device_registry

logger = logging.getLogger(__name__)

AUTH_ENFORCED = os.getenv("SENTRA_AUTH_ENFORCED", "true").strip().lower() in (
    "1", "true", "yes", "on")

REFRESH_TTL_S = 60 * 60 * 24 * 30   # 30 days

OPEN_PATHS = {
    "/api/v1/ping",
    "/api/v1/auth/session",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/pair",
    "/docs", "/redoc", "/openapi.json",
    "/api/v1/docs", "/api/v1/openapi.json",
}

ROLE_PERMISSIONS = {
    "OWNER": ["TELEMETRY_READ", "MANUAL_CONTROL", "ESTOP_TRIGGER",
              "SETTINGS_WRITE", "DEVICES_MANAGE", "EMERGENCY_ACK"],
    "GUARD": ["TELEMETRY_READ", "MANUAL_CONTROL", "ESTOP_TRIGGER",
              "EMERGENCY_ACK"],
    "GUEST": ["TELEMETRY_READ", "EMERGENCY_ACK"],
}


class AuthContext:
    __slots__ = ("device_id", "role", "permissions", "kind", "jti")

    def __init__(self, device_id: str, role: str, permissions: list[str],
                 kind: str, jti: str):
        self.device_id = device_id
        self.role = role
        self.permissions = permissions
        self.kind = kind
        self.jti = jti


def issue_session(device_id: str, role: str) -> dict:
    """Create access+refresh tokens for a device/role and track them."""
    permissions = ROLE_PERMISSIONS.get(role)
    if permissions is None:
        raise ValueError("invalid role")
    dev = device_registry.upsert_device(device_id)
    kind = dev.get("kind", "user")
    if dev.get("revoked"):
        raise PermissionError("device is revoked")

    jti = secrets.token_hex(8)
    refresh_token = secrets.token_urlsafe(32)
    now = int(time.time())
    payload = {
        "sub": device_id,
        "role": role,
        "permissions": permissions,
        "kind": kind,
        "jti": jti,
        "iat": now,
        "exp": now + settings.jwt_expire_seconds,
    }
    token = jwt.encode(payload, settings.jwt_secret_key,
                       algorithm=settings.jwt_algorithm)
    device_registry.register_token(jti, device_id,
                                   ttl_s=settings.jwt_expire_seconds,
                                   refresh_token=refresh_token)
    logger.info("Session issued  device=%s  role=%s  kind=%s", device_id, role, kind)
    return {
        "token": token,
        "refresh_token": refresh_token,
        "role": role,
        "permissions": permissions,
        "expires_in": settings.jwt_expire_seconds,
    }


def rotate_session(refresh_token: str) -> dict:
    """Refresh-token rotation: old jti revoked, new pair issued."""
    found = device_registry.find_by_refresh(refresh_token)
    if found is None:
        raise PermissionError("invalid refresh token")
    device_registry.revoke_token(found["jti"])
    dev = device_registry.get_device(found["device_id"])
    if dev is None or dev.get("revoked"):
        raise PermissionError("device is revoked")
    # role defaults to GUARD for rotating devices unless previously OWNER.
    # (Roles are chosen at login; rotation preserves the device, not the role,
    # so the app should re-login to change role.)
    return issue_session(found["device_id"], "GUARD")


def verify_token(token: str) -> AuthContext:
    """Decode + validate a JWT; raises JWTError/PermissionError on failure."""
    payload = jwt.decode(token, settings.jwt_secret_key,
                         algorithms=[settings.jwt_algorithm])
    jti = payload.get("jti", "")
    state = device_registry.token_state(jti)
    if state == "revoked":
        raise PermissionError("token revoked")
    if state == "expired":
        raise PermissionError("token expired (idle timeout)")
    if state == "unknown":
        # Token from before this registry existed → treat as expired legacy.
        raise PermissionError("token not recognized")
    device_registry.touch_token(jti)
    return AuthContext(
        device_id=payload.get("sub", ""),
        role=payload.get("role", "GUEST"),
        permissions=list(payload.get("permissions", [])),
        kind=payload.get("kind", "user"),
        jti=jti,
    )


def decode_claims(token: str) -> dict:
    """Decode claims without registry checks (logout needs expired tokens too).
    Raises JWTError on malformed tokens."""
    return jwt.decode(token, settings.jwt_secret_key,
                      algorithms=[settings.jwt_algorithm])


def _extract_bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def enforce_auth(request: Request) -> AuthContext:
    """FastAPI dependency: gate mutating endpoints behind a valid JWT."""
    if not AUTH_ENFORCED:
        return AuthContext(device_id="enforcement-off", role="OWNER",
                           permissions=["*"], kind="user", jti="-")
    if request.method in ("GET", "HEAD", "OPTIONS"):
        # Read-only endpoints stay open this phase (Flutter contract).
        return AuthContext(device_id="read-only", role="GUEST",
                           permissions=["TELEMETRY_READ"], kind="user", jti="-")
    if request.url.path in OPEN_PATHS:
        return _open_context(request.url.path)
    token = _extract_bearer(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        ctx = verify_token(token)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=str(exc))
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")
    if "MANUAL_CONTROL" not in ctx.permissions and "SETTINGS_WRITE" not in ctx.permissions \
            and "DEVICES_MANAGE" not in ctx.permissions:
        # GUEST may not drive mutations beyond emergency ack endpoints.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Role lacks mutation permission")
    return ctx


def _open_context(path: str) -> AuthContext:
    return AuthContext(device_id="open", role="GUEST",
                       permissions=["TELEMETRY_READ"], kind="user", jti="-")


async def enforce_ws_control(websocket: WebSocket) -> Optional[AuthContext]:
    """WebSocket gate for /ws/control: token via ?token= or subprotocol.
    Returns None if access is denied (this function closes the socket)."""
    if not AUTH_ENFORCED:
        return AuthContext(device_id="enforcement-off", role="OWNER",
                           permissions=["*"], kind="user", jti="-")
    token = websocket.query_params.get("token")
    if not token:
        subprotocols = websocket.scope.get("subprotocols") or []
        for sp in subprotocols:
            if isinstance(sp, str) and sp.startswith("bearer."):
                token = sp[7:]
                break
    if token is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="Missing token")
        return None
    try:
        ctx = verify_token(token)
    except PermissionError as exc:
        await websocket.accept()
        await websocket.close(code=4401, reason=str(exc))
        return None
    except JWTError:
        await websocket.accept()
        await websocket.close(code=4401, reason="Invalid token")
        return None
    if "MANUAL_CONTROL" not in ctx.permissions:
        await websocket.accept()
        await websocket.close(code=4403, reason="Role lacks MANUAL_CONTROL")
        return None
    return ctx


def require_permission(ctx: AuthContext, permission: str) -> None:
    if permission not in ctx.permissions and "*" not in ctx.permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Missing permission: {permission}")
