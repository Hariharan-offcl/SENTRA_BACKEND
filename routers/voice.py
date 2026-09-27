"""
Router: Voice command endpoints (Phase 9)
  POST /api/v1/voice/command    — structured command intake (or free-text 'raw')
  GET  /api/v1/voice/commands   — command list + examples (app discovery)

Speech-to-text lives in the Flutter app; the backend receives STRUCTURED
commands and resolves targets through the tag map (fuzzy, case-insensitive).
"""

import logging

from fastapi import APIRouter

from models.voice import VoiceCommandRequest, VoiceCommandResponse, VoiceCommandsListResponse
from services import voice_service

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])
logger = logging.getLogger(__name__)


@router.post("/command", response_model=VoiceCommandResponse)
def voice_command(body: VoiceCommandRequest):
    """
    Execute a spoken-style command.

    Examples:
        {"command": "go_to", "target": "kitchen"}
        {"raw": "sentra go to kitchen"}      ← free-text convenience
        {"command": "start_patrol"}
        {"command": "stop"}
        {"command": "go_to_dock"}
        {"command": "call_user", "target": "family"}
    """
    result = voice_service.execute(body.command, body.target, body.raw)
    if not result.get("handled"):
        logger.info("Voice command not handled: %s", result)
    else:
        logger.info("Voice command: %s → %s", result.get("command"), result.get("action"))
    return VoiceCommandResponse(**result)


@router.get("/commands", response_model=VoiceCommandsListResponse)
def voice_commands():
    return VoiceCommandsListResponse(
        commands=list(voice_service.COMMANDS),
        examples={
            "go_to": '{"command":"go_to","target":"kitchen"}',
            "go_to_dock": '{"command":"go_to_dock"}',
            "start_patrol": '{"command":"start_patrol"}',
            "stop": '{"command":"stop"}',
            "call_user": '{"command":"call_user","target":"family"}',
            "raw": '{"raw":"sentra kitchen"}',
        },
    )
