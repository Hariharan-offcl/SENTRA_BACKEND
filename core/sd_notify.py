"""
SENTRA — systemd sd_notify client + watchdog heartbeat (Phase 20).

Minimal pure-stdlib implementation (no systemd Python package needed):

  * `notify(state)` sends a state string to the socket $NOTIFY_SOCKET
    (abstract namespace `@…` or filesystem path), when running under
    systemd with `Type=notify`.
  * `WatchdogHeartbeat` pings `WATCHDOG=1` every WATCHDOG_USEC/2 —
    if the process wedges (GIL-starved threads, stuck C extension),
    systemd kills and restarts it per the unit's WatchdogSec.

Everything is a no-op when $NOTIFY_SOCKET is unset (bare `python main.py`,
dev runs, tests): functions return False / the thread does not start, so
the app behaves identically off-systemd. Log lines say what happened.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

_notify_sock: socket.socket | None = None


def _connect() -> socket.socket | None:
    global _notify_sock
    if _notify_sock is not None:
        return _notify_sock
    addr = os.getenv("NOTIFY_SOCKET", "").strip()
    if not addr:
        return None  # not running under systemd (Type=notify)
    if addr.startswith("@"):  # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        _notify_sock = s
        return s
    except OSError as exc:
        logger.warning("sd_notify socket unavailable (%s) — continuing", exc)
        return None


def notify(state: str) -> bool:
    """Send an sd_notify state string; False when not under systemd."""
    s = _connect()
    if s is None:
        return False
    try:
        s.sendall(state.encode("utf-8"))
        return True
    except OSError as exc:
        logger.warning("sd_notify send failed (%s) — continuing", exc)
        return False


class WatchdogHeartbeat:
    """
    Background `WATCHDOG=1` pinger for systemd WatchdogSec.

    start()/stop() are idempotent; the thread is a daemon so it never holds
    the process open. `running` reflects reality for status endpoints.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False

    def start(self) -> bool:
        if self.running:
            return True
        usec = os.getenv("WATCHDOG_USEC", "").strip()
        if not usec or not usec.isdigit():
            return False  # no WatchdogSec configured → nothing to do
        interval = max(int(usec) / 1_000_000.0 / 2.0, 0.5)

        def _loop() -> None:
            while not self._stop.wait(interval):
                if notify("WATCHDOG=1"):
                    logger.debug("watchdog ping (WATCHDOG=1)")

        self._stop.clear()
        self._thread = threading.Thread(target=_loop, name="sentra-watchdog",
                                        daemon=True)
        self._thread.start()
        self.running = True
        logger.info("Watchdog heartbeat started (every %.1fs, WatchdogSec=%ss)",
                    interval, int(usec) / 1_000_000)
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self.running = False
        logger.info("Watchdog heartbeat stopped")


heartbeat = WatchdogHeartbeat()
