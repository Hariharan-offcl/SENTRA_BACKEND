"""
SENTRA — Startup security hardening checks (Phase 20).

Runs once in lifespan startup and logs findings loudly. This is a *check*
layer, not enforcement: the backend must always come up (it is an assistance
device for elderly users — bricking the rover over a default secret would be
worse than the risk), so findings are surfaced instead of aborting startup.

Checks (details added as risks are identified):
  1. JWT secret is the shipped default → anyone can mint OWNER tokens.
  2. `SENTRA_AUTH_ENFORCED=false` → mutating REST endpoints are open.
  3. Relay enabled with the default shared secret.
  4. Relay enabled over cleartext `ws://` (tokens cross the open internet).

Findings are returned by `run_checks()` and merged into
`GET /api/v1/system/status` (security block) for remote visibility.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_JWT_SECRET = "SENTRA_SUPER_SECRET_CHANGE_IN_PRODUCTION"
DEFAULT_RELAY_SECRET = "sentra-relay-dev-secret"


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def run_checks() -> list[dict]:
    """Evaluate all checks; returns structured findings (empty = clean)."""
    findings: list[dict] = []

    jwt_secret = os.getenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET)
    if jwt_secret == DEFAULT_JWT_SECRET:
        findings.append({
            "id": "JWT_DEFAULT_SECRET",
            "severity": "WARNING",
            "detail": "JWT_SECRET_KEY is the shipped default — set a real "
                      "secret in .env (deploy/install.sh generates one)",
        })

    if not _truthy("SENTRA_AUTH_ENFORCED", "true"):
        findings.append({
            "id": "AUTH_DISABLED",
            "severity": "WARNING",
            "detail": "SENTRA_AUTH_ENFORCED=false — mutating REST endpoints "
                      "are unauthenticated",
        })

    # Phase 4: open-password mode is now opt-in — flag it when active.
    from core.auth import APP_PASSWORD
    if not APP_PASSWORD:
        findings.append({
            "id": "NO_PASSWORD_CHECK",
            "severity": "WARNING",
            "detail": "SENTRA_PASSWORD is not set — any password logs in "
                      "(single-trust appliance mode). Set SENTRA_PASSWORD "
                      "to require it at login.",
        })

    relay_url = os.getenv("SENTRA_RELAY_URL", "").strip()
    if relay_url:
        if os.getenv("SENTRA_RELAY_UNIT_SECRET", DEFAULT_RELAY_SECRET) == DEFAULT_RELAY_SECRET:
            findings.append({
                "id": "RELAY_DEFAULT_SECRET",
                "severity": "WARNING",
                "detail": "SENTRA_RELAY_UNIT_SECRET is the shipped default — "
                          "anyone knowing it can bind to your relay",
            })
        if relay_url.startswith("ws://"):
            findings.append({
                "id": "RELAY_CLEARTEXT",
                "severity": "WARNING",
  "detail": "SENTRA_RELAY_URL uses cleartext ws:// — access tokens cross "
                      "the network unencrypted; use wss:// in production",
        })

    for f in findings:
        logger.warning("SECURITY: [%s] %s", f["id"], f["detail"])
    if not findings:
        logger.info("SECURITY: startup checks passed (no findings)")
    return findings


def summary() -> dict:
    """Compact view for /system/status.security."""
    findings = run_checks()
    return {
        "checked_at_startup": True,
        "findings": findings,
        "count": len(findings),
        "clean": not findings,
    }
