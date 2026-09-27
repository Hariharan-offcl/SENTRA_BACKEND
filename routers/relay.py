"""
Router: Relay (remote access) endpoints (Phase 16)
  GET  /api/v1/relay/status — tunnel state (no secret material)
  POST /api/v1/relay/probe  — run one request through the local dispatch path
                              (verifies the ASGI bridge without a relay)

The tunnel itself is configured by env (SENTRA_RELAY_URL) and runs as a
lifespan task; see services/relay_client.py and tools/relay_server.py.
"""

import logging

from fastapi import APIRouter

from models.relay import RelayProbeRequest, RelayProbeResponse
from services import relay_client

router = APIRouter(prefix="/api/v1/relay", tags=["Relay"])
logger = logging.getLogger(__name__)


@router.get("/status")
def relay_status():
    return relay_client.get_status()


@router.post("/probe", response_model=RelayProbeResponse)
def relay_probe(body: RelayProbeRequest = None):
    """Dispatch one request through the local ASGI bridge (self-test)."""
    import asyncio
    import base64
    body = body or RelayProbeRequest()
    try:
        result = asyncio.run(relay_client.dispatch_local(
            body.method, body.path, {}, {}, base64.b64decode(body.body_b64)))
        return RelayProbeResponse(ok=True, status=result["status"])
    except Exception as exc:
        return RelayProbeResponse(ok=False, detail=str(exc))
