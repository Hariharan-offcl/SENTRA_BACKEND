"""
SENTRA — Notification models (Phase 14).
"""

from pydantic import BaseModel, Field
from typing import Dict, List, Optional


class NotificationItem(BaseModel):
    id: str
    title: str
    timestamp: str = Field(..., description="'<HH:MM:SS> • <Location>' display string")
    description: str
    severity: str
    acknowledged: bool
    event_type: str
    location: str
    created_at: float
    acknowledged_at: Optional[float] = None


class NotificationListResponse(BaseModel):
    notifications: List[NotificationItem]
    unread: int


class NotificationStatsResponse(BaseModel):
    enabled: bool
    total: int
    unread: int
    by_severity: Dict[str, int]
    path: str


class NotificationAckAllResponse(BaseModel):
    ok: bool
    acknowledged: int


class NotificationTestRequest(BaseModel):
    title: str = "Test notification"
    description: str = "Sent from the notifications test endpoint"
    severity: str = "INFO"
