"""
Router: Alerts REST endpoints
  GET  /api/v1/alerts?severity=ALL|DANGER|WARNING
  POST /api/v1/alerts/{id}/ack
"""

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Query

from models.responses import AlertItem, AlertsListResponse, AckAlertResponse

router = APIRouter(prefix="/api/v1/alerts", tags=["Alerts"])
logger = logging.getLogger(__name__)

# ── In-memory alert store (replace with SQLite on Pi for persistence) ─────────

_alerts: List[dict] = [
    {
        "id": "ALT-101",
        "title": "Motion Anomaly Detected",
        "timestamp": "16:32:04 • Living Room",
        "description": "Humanoid motion pattern detected near south window. Confidence: 94%.",
        "severity": "DANGER",
        "acknowledged": False,
    },
    {
        "id": "ALT-102",
        "title": "Battery Low Threshold",
        "timestamp": "15:10:22 • Docking Bay",
        "description": "Battery dropped below 20%. Robot returning to charger.",
        "severity": "WARNING",
        "acknowledged": True,
    },
]


def _add_alert(title: str, description: str, severity: str, location: str = "Unknown") -> dict:
    """Internal helper to push a new alert (called by AI/sensor integrations)."""
    ts = time.strftime("%H:%M:%S")
    alert = {
        "id": f"ALT-{int(time.time())}",
        "title": title,
        "timestamp": f"{ts} • {location}",
        "description": description,
        "severity": severity,
        "acknowledged": False,
    }
    _alerts.append(alert)
    return alert


@router.get("", response_model=AlertsListResponse)
def get_alerts(severity: Optional[str] = Query(default="ALL")):
    """
    Retrieve alert history.
    Filter by severity=DANGER | WARNING | ALL (default).
    """
    if severity and severity.upper() != "ALL":
        filtered = [a for a in _alerts if a["severity"] == severity.upper()]
    else:
        filtered = _alerts

    return AlertsListResponse(alerts=[AlertItem(**a) for a in filtered])


@router.post("/{alert_id}/ack", response_model=AckAlertResponse)
def acknowledge_alert(alert_id: str):
    """Mark a specific alert as acknowledged."""
    for alert in _alerts:
        if alert["id"] == alert_id:
            alert["acknowledged"] = True
            logger.info("Alert %s acknowledged", alert_id)
            return AckAlertResponse(alert_id=alert_id, acknowledged=True)

    return AckAlertResponse(alert_id=alert_id, acknowledged=False)
