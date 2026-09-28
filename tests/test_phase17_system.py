"""
SENTRA — Phase 17 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase17_system.py

Covers: system_metrics service (psutil reads, null-tolerant fallbacks,
no-psutil degraded mode, vcgencmd Pi path + probe cache), router handler,
auth interaction (GET passes open), end-to-end ASGI via relay dispatch
bridge, app integration (89 openapi paths).
"""

import asyncio
import importlib
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


print("\n[1] setup")
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
os.environ["SENTRA_DEVICES_PATH"] = os.path.join(tempfile.mkdtemp(), "devices.json")

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

import services.system_metrics as sm

print("\n[2] service layer — real psutil snapshot")
st = sm.get_system_status()
check("top-level keys", {"timestamp", "uptime_s", "cpu", "memory", "swap",
                         "disk", "temperature_c", "process", "simulation",
                         "source"}
      <= set(st.keys()), str(sorted(st.keys())))
check("simulation block present", isinstance(st.get("simulation"), dict)
      and st["simulation"].get("env_var") == "SENTRA_SIMULATION",  # Phase 18
      str(st.get("simulation")))
check("source is psutil", st["source"] == "psutil", str(st["source"]))
check("timestamp/uptime are ints", isinstance(st["timestamp"], int)
      and isinstance(st["uptime_s"], int) and st["uptime_s"] >= 0)
check("cpu.percent in 0..100", st["cpu"]["percent"] is None
      or 0.0 <= st["cpu"]["percent"] <= 100.0, str(st["cpu"]))
check("cpu.count >= 1", (st["cpu"]["count"] or 0) >= 1, str(st["cpu"]))
check("memory real", st["memory"]["total_mb"] and st["memory"]["total_mb"] > 100
      and 0 <= st["memory"]["percent"] <= 100, str(st["memory"]))
check("swap shape", st["swap"]["percent"] is None
      or 0 <= st["swap"]["percent"] <= 100, str(st["swap"]))
check("disk real", st["disk"]["total_gb"] and st["disk"]["total_gb"] > 1
      and 0 <= st["disk"]["percent"] <= 100, str(st["disk"]))
check("process rss > 0", (st["process"]["rss_mb"] or 0) > 0, str(st["process"]))
check("process threads >= 1", (st["process"]["threads"] or 0) >= 1,
      str(st["process"]))
check("temperature None or float", st["temperature_c"] is None
      or isinstance(st["temperature_c"], float), str(st["temperature_c"]))

print("\n[3] vcgencmd Pi path + probe cache")


class _FakeProc(types.SimpleNamespace):
    pass


_real_run = sm.subprocess.run


def _fake_vcgencmd_run(cmd, **kwargs):
    assert cmd == ["vcgencmd", "measure_temp"]
    return _FakeProc(returncode=0, stdout="temp=52.3'C\n", stderr="")


sm.subprocess.run = _fake_vcgencmd_run
sm._vcgencmd_ok = None
t = sm._read_temp_vcgencmd()
check("vcgencmd parse temp", t == 52.3, str(t))
check("probe cache set True", sm._vcgencmd_ok is True)
sm.subprocess.run = _real_run
t2 = sm._read_temp_vcgencmd()
check("failed probe caches False (short-circuit)", t2 is None
      and sm._vcgencmd_ok is False, f"t2={t2} ok={sm._vcgencmd_ok}")

# temperature aggregator prefers last good reading
sm._last_temp_c = 51.0
check("read_soc_temperature returns cached good value",
      sm.read_soc_temperature() == 51.0)
sm._last_temp_c = None

print("\n[4] degraded mode (no psutil)")
_real_psutil = sm.psutil
sm.psutil = None
st2 = sm.get_system_status()
check("source simulated", st2["source"] == "simulated", str(st2["source"]))
check("all readouts null", st2["memory"]["total_mb"] is None
      and st2["disk"]["percent"] is None and st2["cpu"]["percent"] is None
      and st2["temperature_c"] is None, str(st2))
check("degraded snapshot keeps simulation block",
      st2.get("simulation", {}).get("env_var") == "SENTRA_SIMULATION",  # Phase 18
      str(st2.get("simulation")))
check("uptime still tracked", st2["uptime_s"] >= 0 and st2["cpu"]["count"] >= 1)
sm.psutil = _real_psutil

print("\n[5] router handler")
from routers.system import system_status
resp = system_status()
check("handler returns SystemStatusResponse", resp.source in ("psutil", "simulated")
      and resp.memory.total_mb is not None and resp.disk.total_gb is not None,
      str(resp.source))

print("\n[6] end-to-end ASGI (dispatch bridge incl. middleware)")
import services.relay_client as rc
from main import app  # noqa: F401

r = asyncio.run(rc.dispatch_local("GET", "/api/v1/system/status", None, {}, b""))
import json as _json
body = _json.loads(__import__("base64").b64decode(r["body_b64"]))
check("GET /system/status → 200", r["status"] == 200, str(r["status"]))
check("E2E body has real memory", body.get("memory", {}).get("total_mb", 0) > 100,
      str(body.get("memory")))
check("GET passes without auth token", r["status"] == 200)  # GET/HEAD open per Phase 15

print("\n[7] app integration")
schema_paths = set(app.openapi()["paths"].keys())
check("/api/v1/system/status registered", "/api/v1/system/status" in schema_paths)
check("earlier phases intact",
      {"/api/v1/system/info", "/api/v1/ping", "/api/v1/relay/status"}
      <= schema_paths)
check("path count >= 90 (Phase 18 owns exact count)",
      len(schema_paths) >= 90, str(len(schema_paths)))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
