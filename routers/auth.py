"""
Router: Auth endpoint
  POST /api/v1/auth/session
"""

import logging
import time

from fastapi import APIRouter, HTTPException
from jose import jwt

from config import settings
from models.requests import AuthSessionRequest
from models.responses import AuthSessionResponse

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])
logger = logging.getLogger(__name__)

ROLE_PERMISSIONS = {
    "OWNER": ["TELEMETRY_READ", "MANUAL_CONTROL", "ESTOP_TRIGGER", "SETTINGS_WRITE"],
    "GUARD": ["TELEMETRY_READ", "MANUAL_CONTROL", "ESTOP_TRIGGER"],
    "GUEST": ["TELEMETRY_READ"],
}


@router.post("/session", response_model=AuthSessionResponse)
def create_session(body: AuthSessionRequest):
    """Create a JWT session token bound to the selected role."""
    permissions = ROLE_PERMISSIONS.get(body.role)
    if permissions is None:
        raise HTTPException(status_code=400, detail="Invalid role")

    expire = int(time.time()) + settings.jwt_expire_seconds
    payload = {
        "sub": body.device_id,
        "role": body.role,
        "permissions": permissions,
        "exp": expire,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    logger.info("Session created  device=%s  role=%s", body.device_id, body.role)

    return AuthSessionResponse(
        token=token,
        role=body.role,
        permissions=permissions,
        expires_in=settings.jwt_expire_seconds,
    )
