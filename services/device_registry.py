"""
SENTRA — Device registry (Phase 15).

Persistent registry of paired/known devices (rover phone = "node",
caregiver phones = "user") plus the token table used for per-device
revocation. Designed to scale horizontally:

    - N devices per kind (multiple caregivers, multiple nodes)
    - every issued JWT is tracked by jti → revocable individually
      (logout) or wholesale (revoke a stolen device)
    - refresh tokens are stored hashed; access tokens are stateless
      JWTs whose jti must still be present (not revoked)
    - state is plain JSON with atomic writes (~/sentra_data/devices.json);
      swap for SQLite/Redis later without touching callers

Env vars:
    SENTRA_DEVICES_PATH        (default ~/sentra_data/devices.json)
    SENTRA_AUTH_ENFORCED       (default true) master switch for enforcement
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Optional
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), "sentra_data", "devices.json")
PATH = os.path.expanduser(os.getenv("SENTRA_DEVICES_PATH", DEFAULT_PATH))
TOKEN_TTL_S = 60 * 60 * 24          # access token sliding window (idle expiry)
CLEANUP_GRACE_S = 60 * 60 * 24 * 7  # keep revoked/expired rows 7 days for audit

_lock = threading.RLock()
_devices: dict[str, dict] = {}      # device_id → device record
_tokens: dict[str, dict] = {}       # jti → {device_id, issued_at, last_seen, expires_at, revoked, refresh_hash}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persist_locked() -> None:
    try:
        tmp = PATH + ".tmp"
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "devices": list(_devices.values()),
                       "tokens": _tokens}, f, ensure_ascii=False)
        os.replace(tmp, PATH)
    except Exception as exc:
        logger.error("Device registry persist failed: %s", exc)


def load(path: str | None = None) -> None:
    global PATH
    if path:
        PATH = os.path.expanduser(path)
    with _lock:
        _devices.clear()
        _tokens.clear()
        try:
            with open(PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("devices", []):
                _devices[d["device_id"]] = d
            _tokens.update(data.get("tokens", {}))
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Device registry load failed (starting fresh): %s", exc)
    logger.info("Device registry: %d device(s), %d token(s)",
                len(_devices), len(_tokens))


# ── Devices ──────────────────────────────────────────────────────────────────

def upsert_device(device_id: str, name: str = "", kind: Optional[str] = None,
                  platform: str = "", role: Optional[str] = None) -> dict:
    """Register/update a device. Kind: 'node' (rover phone) | 'user'.
    kind=None (the default) never changes an existing device's kind —
    Phase 21 fix: core.auth.issue_session() calls this bare, which used to
    clobber kind='node' back to 'user' on every token issue.
    role=None likewise never changes the stored role — issue_session passes
    it so rotate_session can re-issue with the device's ORIGINAL role instead
    of downgrading everything to GUARD."""
    now = time.time()
    with _lock:
        d = _devices.get(device_id)
        if d is None:
            d = {"device_id": device_id, "name": name or device_id,
                 "kind": kind if kind in ("node", "user") else "user",
                 "role": role or "GUEST",
                 "platform": platform, "first_seen": now, "last_seen": now,
                 "revoked": False}
            _devices[device_id] = d
        else:
            if name:
                d["name"] = name
            if kind in ("node", "user"):
                d["kind"] = kind
            if role:
                d["role"] = role
            if platform:
                d["platform"] = platform
            d["last_seen"] = now
        rec = dict(d)
        _persist_locked()
    return rec


def get_device(device_id: str) -> Optional[dict]:
    with _lock:
        d = _devices.get(device_id)
        return dict(d) if d else None


def list_devices() -> list[dict]:
    with _lock:
        devices = sorted(_devices.values(), key=lambda d: d.get("last_seen", 0),
                         reverse=True)
        return [dict(d) for d in devices]


def set_device_revoked(device_id: str, revoked: bool = True) -> Optional[dict]:
    """Revoke/unrevoke a device. Revoking also kills all its live tokens."""
    with _lock:
        d = _devices.get(device_id)
        if d is None:
            return None
        d["revoked"] = revoked
        if revoked:
            now = time.time()
            for jti, t in _tokens.items():
                if t["device_id"] == device_id and not t["revoked"]:
                    t["revoked"] = True
                    t["revoked_at"] = now
        rec = dict(d)
        _persist_locked()
        return rec


def delete_device(device_id: str) -> bool:
    with _lock:
        if device_id not in _devices:
            return False
        del _devices[device_id]
        now = time.time()
        for t in _tokens.values():
            if t["device_id"] == device_id and not t.get("revoked"):
                t["revoked"] = True
                t["revoked_at"] = now
        _persist_locked()
        return True


# ── Tokens (tracked by jti) ──────────────────────────────────────────────────

def register_token(jti: str, device_id: str, ttl_s: int,
                   refresh_token: str | None = None) -> None:
    now = time.time()
    with _lock:
        _tokens[jti] = {
            "device_id": device_id,
            "issued_at": now,
            "last_seen": now,
            "expires_at": now + max(60, TOKEN_TTL_S),
            "revoked": False,
            "refresh_hash": _hash(refresh_token) if refresh_token else None,
        }
        _persist_locked()


def touch_token(jti: str) -> None:
    """Sliding idle expiry: successful authenticated use extends the window."""
    now = time.time()
    with _lock:
        t = _tokens.get(jti)
        if t and not t["revoked"]:
            t["last_seen"] = now
            t["expires_at"] = now + TOKEN_TTL_S


def token_state(jti: str) -> str:
    """'active' | 'revoked' | 'expired' | 'unknown'."""
    with _lock:
        t = _tokens.get(jti)
        if t is None:
            return "unknown"
        if t["revoked"]:
            return "revoked"
        if time.time() > t["expires_at"]:
            return "expired"
        return "active"


def find_by_refresh(refresh_token: str) -> Optional[dict]:
    """jti for a refresh token (hashed lookup). None if unknown/revoked."""
    rh = _hash(refresh_token)
    with _lock:
        for jti, t in _tokens.items():
            if t.get("refresh_hash") == rh:
                if t["revoked"]:
                    return None
                return {"jti": jti, "device_id": t["device_id"]}
    return None


def revoke_token(jti: str) -> bool:
    with _lock:
        t = _tokens.get(jti)
        if t is None or t["revoked"]:
            return False
        t["revoked"] = True
        t["revoked_at"] = time.time()
        _persist_locked()
        return True


def revoke_device_tokens(device_id: str) -> int:
    with _lock:
        now = time.time()
        n = 0
        for t in _tokens.values():
            if t["device_id"] == device_id and not t["revoked"]:
                t["revoked"] = True
                t["revoked_at"] = now
                n += 1
        if n:
            _persist_locked()
        return n


def list_tokens(device_id: str | None = None) -> list[dict]:
    with _lock:
        out = []
        for jti, t in _tokens.items():
            if device_id and t["device_id"] != device_id:
                continue
            out.append({"jti": jti, **{k: v for k, v in t.items()
                                       if k != "refresh_hash"}})
        out.sort(key=lambda t: t["issued_at"], reverse=True)
        return out


def cleanup(max_age_s: int = CLEANUP_GRACE_S) -> int:
    """Drop long-dead token rows (audit grace period). Returns rows removed."""
    cutoff = time.time() - max_age_s
    with _lock:
        dead = [j for j, t in _tokens.items()
                if t["revoked"] and t.get("revoked_at", 0) < cutoff]
        for j in dead:
            del _tokens[j]
        if dead:
            _persist_locked()
        return len(dead)


def stats() -> dict:
    with _lock:
        kinds = {"node": 0, "user": 0}
        for d in _devices.values():
            kinds[d.get("kind", "user")] = kinds.get(d.get("kind", "user"), 0) + 1
        active = sum(1 for t in _tokens.values()
                     if not t["revoked"] and time.time() <= t["expires_at"])
    return {
        "devices_total": len(_devices),
        "devices_by_kind": kinds,
        "tokens_tracked": len(_tokens),
        "tokens_active": active,
        "path": PATH,
    }
