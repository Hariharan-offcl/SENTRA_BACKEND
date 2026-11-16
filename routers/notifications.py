"""
Router: Notification endpoints (Phase 14)
  GET  /api/v1/notifications?severity=ALL&limit=100 — full-fidelity feed + unread
  POST /api/v1/notifications/ack-all                — acknowledge everything
  GET  /api/v1/notifications/stats                  — counts + persistence info
  POST /api/v1/notifications/test                   — inject one (dev)

The Flutter Alerts screen keeps using /api/v1/alerts (legacy-compatible
view); this router exposes the richer notification objects.
"""

import logging

from fastapi import APIRouter, Query

from models.notifications import (
    NotificationItem,
    NotificationListResponse,
    NotificationStatsResponse,
    NotificationAckAllResponse,
    NotificationTestRequest,
)
from services import notification_service

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])
logger = logging.getLogger(__name__)


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    severity: str = Query(default="ALL"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Notification feed (newest first) with unread count."""
    items = notification_service.get(severity=severity, limit=limit)
    return NotificationListResponse(
        notifications=[NotificationItem(**n) for n in items],
        unread=notification_service.unread_count(),
    )


@router.post("/ack-all", response_model=NotificationAckAllResponse)
def ack_all():
    """Acknowledge all notifications (returns how many changed)."""
    count = notification_service.ack_all()
    logger.info("Notifications ack-all: %d acknowledged", count)
    return NotificationAckAllResponse(ok=True, acknowledged=count)


@router.get("/stats", response_model=NotificationStatsResponse)
def notification_stats():
    return NotificationStatsResponse(**notification_service.stats())


@router.post("/test", response_model=NotificationItem)
def push_test(body: NotificationTestRequest = None):
    """Inject a notification end-to-end (store + persist + /ws/alerts push)."""
    body = body or NotificationTestRequest()
    n = notification_service.add_manual(body.title, body.description, body.severity)
    return NotificationItem(**n)
