"""
SENTRA — Relay models (Phase 16).
"""

from pydantic import BaseModel, Field
from typing import Optional


class RelayProbeRequest(BaseModel):
    method: str = Field(default="GET")
    path: str = Field(default="/api/v1/ping")
    body_b64: str = ""


class RelayProbeResponse(BaseModel):
    ok: bool
    status: Optional[int] = None
    detail: str = ""
