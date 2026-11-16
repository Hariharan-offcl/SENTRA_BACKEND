"""
Router: Alerts REST endpoints (Flutter contract — APIS.md §10)
  GET  /api/v1/alerts?severity=ALL|DANGER|WARNING|INFO
  POST /api/v1/alerts/{id}/ack

Since Phase 14 this is backed by the real notification service (persistent,
mirrored from the safety-event stream). The Phase 1 mock store is gone;
response shapes are unchanged, plus notification ids now start with NOTIF-.
"""

import logging

from fastapi import APIRouter, Query

from models.responses import AlertItem, AlertsListResponse, AckAlertResponse
from services import notification_service

router = APIRouter(prefix="/api/v1/alerts", tags=["Alerts"])
logger = logging.getLogger(__name__)


@router.get("", response_model=AlertsListResponse)
def get_alerts(
    severity: str = Query(default="ALL"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    Retrieve notification history (newest first).
    Filter by severity=DANGER | WARNING | INFO | ALL (default).
    """
    items = notification_service.get(severity=severity, limit=limit)
    return AlertsListResponse(alerts=[
        AlertItem(
            id=n["id"],
            title=n["title"],
            timestamp=n["timestamp"],
            description=n["description"],
            severity=n["severity"],
            acknowledged=n["acknowledged"],
        )
        for n in items
    ])


@router.post("/{alert_id}/ack", response_model=AckAlertResponse)
def acknowledge_alert(alert_id: str):
    """Mark a specific notification as acknowledged."""
    ok = notification_service.ack(alert_id)
    if ok:
        logger.info("Notification %s acknowledged via /alerts contract", alert_id)
    return AckAlertResponse(alert_id=alert_id, acknowledged=ok)
