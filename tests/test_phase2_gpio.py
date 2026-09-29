"""
SENTRA — Phase 2 GPIO consolidation tests.

Run:
    python -X utf8 tests/test_phase2_gpio.py

Proves the Phase 2 invariants:
  1. gpio_manager is THE single chip handle: one open, claim tracking,
     idempotent per owner, refuses cross-owner conflicts, closes once.
  2. Every GPIO consumer (motor service, HAL motor, cliff, encoder,
     ultrasonic — service AND HAL copies) claims through it. No private
     gpiochip_open anywhere.
  3. The active brake runs on the SAME handle/pins as the drive path
     (no more split-brain between _apply_wheel_duty and apply_active_brake).
  4. Encoder default pins no longer collide with IN4 (23 reserved).
"""

import importlib
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


os.environ.setdefault("SENTRA_SIMULATION", "false")
os.environ.setdefault("SENTRA_ENCODER_LEFT_GPIO", "20")

import core.config as core_config
importlib.reload(core_config)

# ── [1] dev box: manager degrades cleanly, close is one-shot ─────────────────
print("\n[1] gpio_manager on a dev box (no lgpio)")
import hardware.gpio_manager as gm
importlib.reload(gm)

check("chip stays None without lgpio", gm.gpio_manager.chip is None)
check("no claims recorded", gm.gpio_manager.claimed == {})
gm.gpio_manager.close()  # must not raise
gm.gpio_manager.close()  # double close must not raise
check("close() idempotent and safe", gm.gpio_manager.chip is None)

# ── [2] fake lgpio: single open, claim semantics ─────────────────────────────
print("\n[2] one handle, claim tracking (fake lgpio)")
calls = {"open": [], "close": [], "write": [], "pwm": [], "free": []}


class _FakeLgpio(types.ModuleType):
    BOTH_EDGES = 2

    @staticmethod
    def gpiochip_open(chip):
        calls["open"].append(chip)
        return f"handle-{chip}"

    @staticmethod
    def gpiochip_close(h):
        calls["close"].append(h)
        return 0

    @staticmethod
    def gpio_claim_output(h, pin, value=0):
        return 0

    @staticmethod
    def gpio_claim_input(h, pin):
        return 0

    @staticmethod
    def gpio_write(h, pin, value):
        calls["write"].append((pin, value))
        return 0

    @staticmethod
    def gpio_read(h, pin):
        return 0

    @staticmethod
    def tx_pwm(h, pin, freq, duty):
        calls["pwm"].append((pin, duty))
        return 0

    @staticmethod
    def gpio_free(h, pin):
        calls["free"].append(pin)
        return 0

    @staticmethod
    def callback(h, pin, edge, fn):
        return f"cb-{pin}"


sys.modules["lgpio"] = _FakeLgpio("lgpio")

importlib.reload(gm)
h1 = gm.gpio_manager.chip
h2 = gm.gpio_manager.chip
check("handle opens exactly once", len(calls["open"]) == 1 and h1 is h2, str(calls["open"]))
check("claim_output succeeds", gm.gpio_manager.claim_output(12, owner="t1") is True)
check("same-owner re-claim idempotent", gm.gpio_manager.claim_output(12, owner="t1") is True)
check("cross-owner claim refused", gm.gpio_manager.claim_output(12, owner="t2") is False)
check("claim_input tracked", gm.gpio_manager.claim_input(20, owner="t3") is True)
check("claims recorded", gm.gpio_manager.claimed == {12: "t1", 20: "t3"}, str(gm.gpio_manager.claimed))
gm.gpio_manager.close()
check("close releases claims + closes chip once", len(calls["close"]) == 1
      and gm.gpio_manager.claimed == {}, str(calls["close"]))
gm.gpio_manager.claim_output(13, owner="t4")  # re-open after close
check("handle re-opens after close", len(calls["open"]) == 2, str(calls["open"]))
# Clean slate for section [3]: the singleton persists across reloads BY
# DESIGN (that is the consolidation), so release the test claims.
gm.gpio_manager.close()
calls["open"].clear()

# ── [3] every consumer on the shared handle ──────────────────────────────────
print("\n[3] all GPIO consumers share ONE handle")
calls["open"].clear()

import services.motor_service as ms
importlib.reload(ms)  # module-level _init_hardware claims via gpio_manager
check("motor service uses the shared handle", ms._h == gm.gpio_manager.chip and ms._h is not None,
      f"h={ms._h}")
motor_pins = {12, 27, 17, 13, 22, 23}
check("six L298N pins claimed by owner 'motor'",
      all(gm.gpio_manager.claimed.get(p) == "motor" for p in motor_pins),
      str(gm.gpio_manager.claimed))

import services.cliff_service as cs
importlib.reload(cs)
cs._init_hardware()
check("cliff claims on shared handle",
      gm.gpio_manager.claimed.get(19) == "cliff.service"
      and gm.gpio_manager.claimed.get(26) == "cliff.service", str(gm.gpio_manager.claimed))

import services.ultrasonic_service as us
importlib.reload(us)
us._init_hardware()
check("ultrasonic claims on shared handle",
      all(gm.gpio_manager.claimed.get(p) == "ultrasonic.service"
          for p in (24, 25, 5, 6)), str(gm.gpio_manager.claimed))

import services.encoder_service as enc
importlib.reload(enc)
enc._init_hardware()
check("encoder claims on shared handle (pins 20/16)",
      gm.gpio_manager.claimed.get(20) == "encoder.service"
      and gm.gpio_manager.claimed.get(16) == "encoder.service", str(gm.gpio_manager.claimed))
check("encoder refuses IN4 pin 23", enc._pins_collide() is False or 23 not in
      (enc.ENCODER_LEFT_GPIO, enc.ENCODER_RIGHT_GPIO))

total = len(gm.gpio_manager.claimed)
check("all 14 pins on ONE handle, ONE open", total == 14 and len(calls["open"]) == 1,
      f"claims={total} opens={calls['open']}")

# ── [4] HAL + service share the motor claims; brake = same path as drive ────
print("\n[4] HAL/service one-claim + brake on the drive path")
import hardware.motor.physical as motor_physical
importlib.reload(motor_physical)
import hardware.manager as hm_mod
hm_mod.HardwareManager._instance = None  # fresh orchestrator for this test
importlib.reload(hm_mod)
importlib.reload(ms)  # re-run claims (idempotent under owner 'motor')
hm_mod.hardware_manager.initialize()  # HAL switch: simulated → physical

hal_motor = hm_mod.hardware_manager.motor
check("HAL motor is PHYSICAL under fake lgpio", type(hal_motor).__name__ == "PhysicalMotor",
      str(type(hal_motor)))
check("HAL motor init succeeded without double-claim",
      hal_motor.initialized is True and len(calls["open"]) == 1)

# Drive path: safety layer → _apply_wheel_duty → HAL → shared handle
import core.state as core_state
from core.state import RobotState, MANUAL
core_state._robot_state = RobotState()
from core.safety import SafetyLayer
import core.safety as core_safety
sl = SafetyLayer()
sl.wire(motor_apply=ms._apply_wheel_duty, sensor_provider=lambda: {
    "front_distance_m": 2.0, "rear_distance_m": 2.0,
    "left_cliff": False, "right_cliff": False})
sl.wire_event_reporter(lambda *a, **k: None)
core_safety._safety = sl
core_state._robot_state.request_mode(MANUAL, requested_by="gpio-test")

calls["write"].clear()
calls["pwm"].clear()
d = sl.check_and_apply("gpio-test", MANUAL, 40, 40)
check("drive applied through HAL", d.get("applied") is True, str(d))
drove = any(p in (12, 13) for p, _ in calls["pwm"])
check("drive PWM visible on shared handle", drove, str(calls["pwm"]))

# Brake path: mc.brake → apply_active_brake → HAL motor.brake → SAME handle
calls["write"].clear()
calls["pwm"].clear()
from services.motion_controller import get_motion_controller
mc = get_motion_controller()
mc.start()
try:
    mc.brake("gpio-test")
    time_s = 0.0
    import time as _time
    while time_s < 2.0 and not calls["pwm"]:
        _time.sleep(0.05)
        time_s += 0.05
    brake_writes = list(calls["write"])
    shorted = ({27, 17} <= {p for p, v in brake_writes if v == 1}
               and {22, 23} <= {p for p, v in brake_writes if v == 1})
    check("active brake shorted IN1/IN2 and IN3/IN4 (same handle as drive)",
          shorted, str(brake_writes))
    check("brake PWM at full duty on ENA/ENB",
          any(p == 12 and duty == 100 for p, duty in calls["pwm"])
          and any(p == 13 and duty == 100 for p, duty in calls["pwm"]),
          str(calls["pwm"]))
    check("still exactly ONE chip open after drive+brake", len(calls["open"]) == 1,
          str(calls["open"]))
finally:
    mc.shutdown()

# ── [5] restore dev state + app boot ────────────────────────────────────────
print("\n[5] restore + app boot")
sys.modules.pop("lgpio", None)
importlib.reload(gm)
importlib.reload(motor_physical)
hm_mod.HardwareManager._instance = None
importlib.reload(hm_mod)
importlib.reload(ms)
importlib.reload(cs)
importlib.reload(us)
importlib.reload(enc)
check("dev restore: manager handle None again", gm.gpio_manager.chip is None)
check("dev restore: HAL back to simulated motor",
      type(hm_mod.hardware_manager.motor).__name__ == "SimulatedMotor")

from main import app  # noqa: F401
paths = set(app.openapi()["paths"].keys())
check("app still assembles", "/api/v1/ping" in paths and len(paths) >= 120)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
