"""
SENTRA — Phase 18 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase18_simulation.py

Covers: core/simulation.py (env latch, per-service gate, preflight stop,
banner, status), service gating with a FAKE lgpio — proving GPIO init is
refused under SENTRA_SIMULATION=true even when lgpio imports fine (the real
Pi risk scenario) — router handlers, E2E through the dispatch bridge,
app routes (90 openapi paths).
"""

import importlib
import logging
import os
import sys
import tempfile
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


print("\n[1] setup (real mode default)")
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")
os.environ.pop("SENTRA_SIMULATION", None)
# Encoder LEFT default (23) collides with motor IN4 by design — pick a free pin
# so the REAL-mode branch can prove real claims happen.
os.environ.setdefault("SENTRA_ENCODER_LEFT_GPIO", "20")

import services.tag_map as tag_map
tag_map.load(os.path.join(tempfile.mkdtemp(), "tag_map.json"))
import core.state as core_state
from core.state import RobotState
core_state._robot_state = RobotState()
from core.safety import SafetyLayer
import core.safety as core_safety
core_safety._safety = SafetyLayer()
core_safety._safety.wire(motor_apply=lambda l, r: None, sensor_provider=lambda: {})
core_safety._safety.wire_event_reporter(lambda *a, **k: None)

import services.device_registry as dr
importlib.reload(dr)
dr.load()
import core.auth as ca
importlib.reload(ca)

import core.simulation as sim

print("\n[2] real mode (SENTRA_SIMULATION unset)")
check("is_active() False", sim.is_active() is False)
check("per-service override passthrough enabled",
      sim.check_env_override("imu", True) is True)
check("per-service override passthrough disabled",
      sim.check_env_override("imu", False) is False)
check("preflight_stop no-op in real mode", sim.preflight_stop() is None)
check("describe real", sim.describe() == "REAL (hardware enabled where available)",
      sim.describe())
st = sim.status()
check("status: not simulation, not latched",
      st["simulation"] is False and st["latched"] is False
      and st["hardware_disabled"] is False and st["env_var"] == "SENTRA_SIMULATION",
      str(st))
sim.activate_once()
st2 = sim.status()
check("activate_once latches (idempotent flag), mode unchanged",
      st2["latched"] is True and st2["simulation"] is False, str(st2))

print("\n[3] simulated mode (reload with SENTRA_SIMULATION=true)")
os.environ["SENTRA_SIMULATION"] = "true"
import core.simulation as sim_t  # noqa: F401
sim_t = importlib.reload(sim_t)
check("is_active() True after reload", sim_t.is_active() is True)
check("override refused while simulating",
      sim_t.check_env_override("imu", True) is False)
check("override False wins too", sim_t.check_env_override("imu", False) is False)
check("describe simulated",
      sim_t.describe() == "SIMULATION (hardware disabled by config)",
      sim_t.describe())
pre = sim_t.preflight_stop()
check("preflight stop forces motors via safety layer",
      isinstance(pre, dict) and pre.get("forced") == "simulation_preflight"
      and pre.get("applied") is False, str(pre))
recs = []


class _Cap(logging.Handler):
    def emit(self, record):
        recs.append(record.getMessage())


_hnd = _Cap()
logging.getLogger("core.simulation").addHandler(_hnd)
sim_t.banner()
logging.getLogger("core.simulation").removeHandler(_hnd)
check("banner is loud (multiple lines)", len(recs) >= 5, str(len(recs)))
check("banner names the mode",
      any("RUNNING IN SIMULATION" in m for m in recs), str(recs[:2]))
st3 = sim_t.status()
check("status: simulation + hardware_disabled + preflight recorded",
      st3["simulation"] is True and st3["hardware_disabled"] is True
      and (st3["preflight_stop"] or {}).get("forced") == "simulation_preflight",
      str(st3))
sim_t.activate_once()
check("latch recorded in sim mode", sim_t.status()["latched"] is True)

print("\n[4] service gating — FAKE lgpio (proves refusal even when lgpio imports)")
claims: list[str] = []


class _FakeLgpio(types.ModuleType):
    BOTH_EDGES = 2

    @staticmethod
    def gpiochip_open(chip):
        claims.append(f"open({chip})")
        return chip

    @staticmethod
    def gpiochip_close(h):
        return 0

    @staticmethod
    def gpio_claim_output(h, pin, value=0):
        claims.append(f"out({pin})")
        return 0

    @staticmethod
    def gpio_claim_input(h, pin):
        claims.append(f"in({pin})")
        return 0

    @staticmethod
    def gpio_write(h, pin, value):
        return 0

    @staticmethod
    def gpio_read(h, pin):
        return 0

    @staticmethod
    def tx_pwm(h, pin, freq, duty):
        return 0

    @staticmethod
    def callback(h, pin, edge, fn):
        claims.append(f"cb({pin})")
        return object()


sys.modules["lgpio"] = _FakeLgpio("lgpio")

# ── 4a: real mode → pins ARE claimed ─────────────────────────────────────────
os.environ["SENTRA_SIMULATION"] = "false"
import core.config as core_config
importlib.reload(core_config)
check("core_config.SIMULATION False (real)", core_config.SIMULATION is False)

import services.motor_service as ms
importlib.reload(ms)  # auto-inits at import
check("REAL: motor claimed the chip", ms._h is not None and len(claims) >= 6,
      f"h={ms._h} claims={claims}")
import services.cliff_service as cs
importlib.reload(cs)
cs._init_hardware()
check("REAL: cliff initialized (not simulated)", cs._simulated is False)
import services.ultrasonic_service as us
importlib.reload(us)
us._init_hardware()
check("REAL: ultrasonic claimed pins", us._h is not None)
import services.encoder_service as enc
importlib.reload(enc)
enc._init_hardware()
check("REAL: encoders initialized (not simulated)", enc._simulated is False)
real_claim_count = len(claims)
check("REAL: pins actually claimed (motor+cliff+ultra+enc)",
      real_claim_count >= 10, str(claims))

# ── 4b: simulated mode → SAME fake lgpio, ZERO claims ────────────────────────
os.environ["SENTRA_SIMULATION"] = "true"
importlib.reload(core_config)
check("core_config.SIMULATION True (sim)", core_config.SIMULATION is True)
importlib.reload(sim_t)
claims.clear()

importlib.reload(ms)
check("SIM: motor refused init despite lgpio available",
      ms._h is None and ms._LGPIO_AVAILABLE is True, f"h={ms._h}")
check("SIM: zero GPIO claims", len(claims) == 0, str(claims))

importlib.reload(cs)
cs._init_hardware()
check("SIM: cliff stays simulated", cs._simulated is True and cs._h is None)
importlib.reload(us)
us._init_hardware()
check("SIM: ultrasonic refused", us._h is None)
importlib.reload(enc)
enc._init_hardware()
check("SIM: encoders stay simulated", enc._simulated is True)
import services.imu_service as imu
importlib.reload(imu)
check("SIM: IMU I2C bus refused", imu._init_bus() is None)
check("SIM: still zero claims after all inits", len(claims) == 0, str(claims))

print("\n[5] router handlers (sim mode active)")
import routers.simulation as rs
resp = rs.get_simulation()
check("GET /simulation handler → simulation True", resp.simulation is True
      and resp.hardware_disabled is True, str(resp))
check("POST /simulation/status handler mirrors", rs.post_simulation_status()
      .simulation is True)

print("\n[6] restore real mode + re-verify handlers")
os.environ.pop("SENTRA_SIMULATION", None)
sys.modules.pop("lgpio", None)
importlib.reload(core_config)
importlib.reload(sim_t)
importlib.reload(ms)
importlib.reload(cs)
importlib.reload(us)
importlib.reload(enc)
importlib.reload(imu)
importlib.reload(rs)
check("restored: handlers report real mode", rs.get_simulation().simulation is False)

print("\n[7] end-to-end ASGI (dispatch bridge incl. middleware)")
import json as _json
import base64 as _b64
import services.relay_client as rc
from main import app  # noqa: F401

r = asyncio_run = None
import asyncio
r = asyncio.run(rc.dispatch_local("GET", "/api/v1/simulation", None, {}, b""))
body = _json.loads(_b64.b64decode(r["body_b64"]))
check("GET /api/v1/simulation → 200", r["status"] == 200, str(r["status"]))
check("E2E body reports real mode", body.get("simulation") is False
      and body.get("latched") is True, str(body))
r = asyncio.run(rc.dispatch_local("GET", "/api/v1/system/status", None, {}, b""))
body = _json.loads(_b64.b64decode(r["body_b64"]))
check("/system/status carries simulation block",
      body.get("simulation", {}).get("simulation") is False
      and body.get("simulation", {}).get("env_var") == "SENTRA_SIMULATION",
      str(body.get("simulation")))
r = asyncio.run(rc.dispatch_local(
    "POST", "/api/v1/simulation/status", None,
    {"content-type": "application/json"}, b"{}"))
check("POST /simulation/status unauthenticated → 401 (gated, read-only mirror)",
      r["status"] == 401, str(r["status"]))

print("\n[8] app integration")
schema_paths = set(app.openapi()["paths"].keys())
check("/api/v1/simulation registered", "/api/v1/simulation" in schema_paths)
check("/api/v1/simulation/status registered",
      "/api/v1/simulation/status" in schema_paths)
check("earlier phases intact",
      {"/api/v1/system/status", "/api/v1/relay/status", "/api/v1/devices"}
      <= schema_paths)
check("path count >= 121 (Phase 21 owns exact count)",
      len(schema_paths) >= 121, str(len(schema_paths)))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
