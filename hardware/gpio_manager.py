"""
SENTRA — Central GPIO chip handle (Phase 2 consolidation).

ONE lazy-opened lgpio handle shared by the HAL physical drivers AND every
service that writes GPIO (motor / cliff / encoder / ultrasonic). Before this
module the backend mixed two parallel stacks — services opened gpiochip4 on
their own while the HAL opened gpiochip0 — so the active brake and the drive
path could fight over the same pins through different kernel consumers.

Pi 5 note: gpiochip0 and gpiochip4 are two aliases of the same RP1 GPIO
controller, so opening both and claiming the same line twice is exactly the
"line busy" / split-brain hazard this consolidation removes.

Env: SENTRA_GPIOCHIP (default 0) selects the chip number.

Also tracks every claimed line so shutdown can release them in order and
close the chip exactly once.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("sentra.hardware.gpio")


def _lg():
    """Resolve lgpio at CALL time (not import time) so test doubles injected
    into sys.modules are picked up, and dev machines without lgpio degrade
    cleanly instead of failing at import."""
    try:
        import lgpio
        return lgpio
    except ImportError:
        return None

CHIP_NUMBER = int(os.getenv("SENTRA_GPIOCHIP", "0"))


class GPIOManager:
    """Lazy, shared, single-handle owner of the GPIO chip."""

    def __init__(self):
        # RLock: close() holds the lock while calling release_all(), which
        # re-acquires it (a plain Lock would deadlock there).
        self._lock = threading.RLock()
        self._chip = None
        self._open_attempted = False
        self.claimed: dict[int, str] = {}  # pin → owner name

    @property
    def chip(self):
        """The lgpio handle, opened on first access (None when unavailable)."""
        with self._lock:
            if self._chip is None and not self._open_attempted:
                self._open_attempted = True
                self._chip = self._open()
            return self._chip

    def _open(self):
        lgpio = _lg()
        if lgpio is None:
            logger.warning("GPIOManager: lgpio not available (dev machine?) — "
                           "chip handle stays None")
            return None
        try:
            h = lgpio.gpiochip_open(CHIP_NUMBER)
            logger.info("GPIOManager: opened gpiochip %d (shared handle)", CHIP_NUMBER)
            return h
        except Exception as e:
            logger.error("GPIOManager: failed to open gpiochip %d: %s", CHIP_NUMBER, e)
            return None

    def claim_output(self, pin: int, owner: str, initial: int = 0) -> bool:
        """Claim a pin as output under this handle. Idempotent per owner."""
        lgpio = _lg()
        h = self.chip
        if lgpio is None or h is None:
            return False
        with self._lock:
            if pin in self.claimed:
                if self.claimed[pin] == owner:
                    return True
                logger.warning("GPIOManager: pin %d already claimed by %s "
                               "(requested by %s)", pin, self.claimed[pin], owner)
                return False
            try:
                lgpio.gpio_claim_output(h, pin, initial)
                self.claimed[pin] = owner
                return True
            except Exception as exc:
                logger.error("GPIOManager: claim_output(%d) failed: %s", pin, exc)
                return False

    def claim_input(self, pin: int, owner: str) -> bool:
        lgpio = _lg()
        h = self.chip
        if lgpio is None or h is None:
            return False
        with self._lock:
            if pin in self.claimed:
                if self.claimed[pin] == owner:
                    return True
                logger.warning("GPIOManager: pin %d already claimed by %s "
                               "(requested by %s)", pin, self.claimed[pin], owner)
                return False
            try:
                lgpio.gpio_claim_input(h, pin)
                self.claimed[pin] = owner
                return True
            except Exception as exc:
                logger.error("GPIOManager: claim_input(%d) failed: %s", pin, exc)
                return False

    def release_all(self) -> None:
        """Best-effort release of every claimed line (keeps the handle)."""
        lgpio = _lg()
        h = self._chip
        if h is None or lgpio is None:
            self.claimed.clear()
            return
        with self._lock:
            for pin in list(self.claimed):
                try:
                    lgpio.gpio_free(h, pin)
                except Exception:
                    pass
            self.claimed.clear()

    def close(self):
        """Release claims and close the chip exactly once."""
        with self._lock:
            if self._chip is None:
                return
            self.release_all()
            lgpio = _lg()
            if lgpio is not None:
                try:
                    lgpio.gpiochip_close(self._chip)
                    logger.info("GPIOManager: gpiochip closed")
                except Exception as exc:
                    logger.warning("GPIOManager: close error: %s", exc)
            self._chip = None
            self._open_attempted = False


# Singleton instance
gpio_manager = GPIOManager()
