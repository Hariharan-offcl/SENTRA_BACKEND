"""
Router: Auth endpoints (Phase 15)
  POST /api/v1/auth/session  — issue access+refresh JWT pair (OPEN)
  POST /api/v1/auth/refresh  — rotate a refresh token → new pair (OPEN)
  POST /api/v1/auth/logout   — revoke one access token by jti (OPEN)

Phase 1 contract preserved: /session still returns {token, role,
permissions, expires_in} — refresh_token is an additive field. Tokens are
tracked by jti in the device registry, so they are revocable and idle-expire.
"""

import logging

from fastapi import APIRouter, HTTPException

from models.requests import AuthSessionRequest, AuthRefreshRequest, AuthLogoutRequest
from models.responses import (
    AuthSessionResponse,
    AuthRefreshResponse,
    AuthLogoutResponse,
)
from core import auth as core_auth
from services import device_registry

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


@router.post("/session", response_model=AuthSessionResponse)
def create_session(body: AuthSessionRequest):
    """Create a JWT session bound to a role + device (registers the device)."""
    try:
        result = core_auth.issue_session(body.device_id, body.role)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role")
    if body.name or body.kind or body.platform:
        device_registry.upsert_device(body.device_id, name=body.name,
                                      kind=body.kind or "user",
                                      platform=body.platform)
    return AuthSessionResponse(**result)


@router.post("/refresh", response_model=AuthRefreshResponse)
def refresh_session(body: AuthRefreshRequest):
    """Exchange a refresh token for a new pair (old access token revoked)."""
    try:
        result = core_auth.rotate_session(body.refresh_token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return AuthRefreshResponse(**result)


@router.post("/logout", response_model=AuthLogoutResponse)
def logout(body: AuthLogoutRequest = None):
    """Revoke the supplied access token (works even if it already expired)."""
    revoked = 0
    if body and body.token:
        try:
            claims = core_auth.decode_claims(body.token)
            if claims.get("jti") and device_registry.revoke_token(claims["jti"]):
                revoked = 1
        except Exception:
            revoked = 0
    logger.info("Logout: %d token(s) revoked", revoked)
    return AuthLogoutResponse(ok=revoked > 0, revoked=revoked)
