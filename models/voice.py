"""
SENTRA — Voice command models (Phase 9).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class VoiceCommandRequest(BaseModel):
    command: Optional[str] = Field(default=None, max_length=64,
                                   description="go_to | go_to_dock | start_patrol | stop | call_user")
    target: Optional[str] = Field(default=None, max_length=64,
                                  description="Spoken location name for go_to")
    raw: Optional[str] = Field(default=None, max_length=280,
                               description="Free-text utterance; parsed when 'command' omitted")


class VoiceCommandResponse(BaseModel):
    handled: bool
    command: Optional[str] = None
    action: Optional[str] = None
    target: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    known_locations: Optional[List[str]] = None
    commands: Optional[List[str]] = None
    route: Optional[str] = None
    mode: Optional[str] = None
    tag_id: Optional[int] = None
    note: Optional[str] = None
    elapsed_ms: Optional[int] = None


class VoiceCommandsListResponse(BaseModel):
    commands: List[str]
    examples: Dict[str, str]
