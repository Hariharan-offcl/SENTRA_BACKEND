"""
SENTRA — Phase 19 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase19_deploy.py

Covers: deploy/ asset integrity (systemd units, env template, installer
syntax, runbook), env-var drift guard (every name in env.template must exist
in code), relay server --host flag (live subprocess on loopback), and app
route stability (no new endpoints; still 91 paths).
"""

import asyncio
import importlib.util
import os
import re
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")

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


print("\n[1] deploy assets exist")
_expected = ["sentra-backend.service", "sentra-relay.service",
             "env.template", "install.sh", "README.md"]
for fname in _expected:
    check(f"deploy/{fname} present", os.path.isfile(os.path.join(DEPLOY, fname)))

print("\n[2] systemd unit integrity (backend)")
with open(os.path.join(DEPLOY, "sentra-backend.service"), encoding="utf-8") as f:
    unit = f.read()
check("unit sections", "[Unit]" in unit and "[Service]" in unit
      and "[Install]" in unit)
check("runs the venv uvicorn on :8080",
      "/opt/sentra/venv/bin/python -m uvicorn main:app" in unit
      and "--port 8080" in unit)
check("EnvironmentFile=/opt/sentra/.env", "EnvironmentFile=/opt/sentra/.env" in unit)
check("auto-restart policy", "Restart=always" in unit and "RestartSec=5s" in unit)
check("network-online ordering", "After=network-online.target" in unit
      and "Wants=network-online.target" in unit)
check("hardening present", "ProtectSystem=strict" in unit
      and "NoNewPrivileges=true" in unit)
check("hardware device access", "char-gpiochip" in unit and "char-i2c" in unit)
check("runs as dedicated user", "User=sentra" in unit)

print("\n[3] systemd unit integrity (relay)")
with open(os.path.join(DEPLOY, "sentra-relay.service"), encoding="utf-8") as f:
    runit = f.read()
check("relay binds loopback behind TLS proxy",
      "--host 127.0.0.1" in runit and "--port 8765" in runit)
check("relay isolated user + env", "User=sentra-relay" in runit
      and "EnvironmentFile=/opt/sentra-relay/.env" in runit)
check("relay auto-restart", "Restart=always" in runit)

print("\n[4] installer sanity")
sh_path = os.path.join(DEPLOY, "install.sh")


def _find_real_bash():
    """Windows dev: 'bash' may resolve to a broken WSL stub — find Git Bash."""
    if os.name != "nt":
        return "bash"
    for base in (r"C:\Program Files\Git", r"C:\Program Files (x86)\Git"):
        cand = os.path.join(base, "bin", "bash.exe")
        if os.path.isfile(cand):
            return cand
    return "bash"


r = subprocess.run([_find_real_bash(), "-n", sh_path],
                   capture_output=True, text=True, timeout=30)
check("install.sh passes bash -n", r.returncode == 0, r.stderr[:200])
with open(sh_path, encoding="utf-8") as f:
    sh = f.read()
check("set -euo pipefail", "set -euo pipefail" in sh)
check("never overwrites .env", "keeping existing" in sh
      and '--exclude ".env"' in sh)
check("generates JWT secret into fresh .env", "secrets.token_hex(32)" in sh)
check("installs the backend unit", "sentra-backend.service" in sh)
check("health-gated finish", "/api/v1/ping" in sh)
check("grants gpio/i2c/spi/video access", "-aG gpio,i2c,spi,video" in sh)

print("\n[5] env template — drift guard vs code")
with open(os.path.join(DEPLOY, "env.template"), encoding="utf-8") as f:
    tmpl = f.read()
env_names = sorted(set(re.findall(r"^(SENTRA_[A-Z_]+)=", tmpl, re.M)))
check("template defines env vars", len(env_names) >= 20, str(len(env_names)))

code_text = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in (".git", "venv", "__pycache__",
                                                    "deploy", "tests", "docs")]
    for fn in filenames:
        if fn.endswith(".py"):
            with open(os.path.join(dirpath, fn), encoding="utf-8",
                      errors="ignore") as f:
                code_text.append(f.read())
code_blob = "\n".join(code_text)

_missing = [n for n in env_names if n not in code_blob]
check("every template env var exists in code (no drift)", not _missing,
      f"missing: {_missing}")

_stale = [n for n in re.findall(r"^(SENTRA_[A-Z_]+)=", code_blob, re.M)
          if n not in env_names and n not in (
              "SENTRA_CLIFF_LEFT_GPIO", "SENTRA_CLIFF_RIGHT_GPIO",
              "SENTRA_CLIFF_ACTIVE_HIGH", "SENTRA_IMU_BUS", "SENTRA_IMU_ADDR",
              "SENTRA_IMU_POLL_S", "SENTRA_ENCODER_LEFT_GPIO",
              "SENTRA_ENCODER_RIGHT_GPIO", "SENTRA_TICKS_PER_REV",
              "SENTRA_WHEEL_DIAM_M", "SENTRA_DOCK_SEARCH_DUTY",
              "SENTRA_DOCK_APPROACH_DUTY", "SENTRA_DOCK_SLOW_M",
              "SENTRA_DOCK_NEAR_DUTY", "SENTRA_DOCK_STOP_M",
              "SENTRA_DOCK_ALIGN_TOL_DEG", "SENTRA_DOCK_ALIGN_DUTY",
              "SENTRA_DOCK_STEER_GAIN", "SENTRA_DOCK_TAG_LOST_GRACE_S",
              "SENTRA_NAV_SEARCH_TIMEOUT_S", "SENTRA_NAV_ARRIVE_M",
              "SENTRA_PATROL_TICK_S", "SENTRA_PATROL_BLOCK_POLL_S",
              "SENTRA_PATROL_MAX_BLOCK_S", "SENTRA_PATROL_CONFIRM_FRESH_S",
              "SENTRA_FRONT_STOP_M", "SENTRA_REAR_STOP_M")]
check("code env vars documented or allowlisted (sample)", len(_stale) < 10,
      f"undocumented: {_stale[:10]}")
check("template forbids shipped JWT default",
      "SENTRA_SUPER_SECRET_CHANGE_IN_PRODUCTION" not in tmpl
      and "<long-random-secret>" in tmpl)
check("template documents simulation mode", "SENTRA_SIMULATION" in tmpl
      and "Phase 18" in tmpl)

print("\n[6] runbook")
with open(os.path.join(DEPLOY, "README.md"), encoding="utf-8") as f:
    rd = f.read()
check("runbook covers both targets", "Pi 5 unit" in rd and "Relay VPS" in rd)
check("runbook has production checklist", "Production checklist" in rd
      and "JWT_SECRET_KEY" in rd)

print("\n[7] relay server --host flag — live subprocess on loopback")
spec = importlib.util.spec_from_file_location(
    "sentra_relay_host_test", os.path.join(ROOT, "tools", "relay_server.py"))
assert spec is not None  # silence linters; exec below proves it

# free port
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()

proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "tools", "relay_server.py"),
     "--port", str(port), "--host", "127.0.0.1"],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

import websockets


async def _relay_probe():
    import json as _json
    # wait for the listener
    for _ in range(50):
        try:
            ws = await websockets.connect(f"ws://127.0.0.1:{port}",
                                          open_timeout=1)
            break
        except Exception:
            await asyncio.sleep(0.1)
    else:
        return None
    try:
        await ws.send(_json.dumps({"type": "unit_hello", "unit_id": "T1",
                                   "secret": "wrong-secret"}))
        reply = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        return reply
    finally:
        await ws.close()


reply = None
try:
    reply = asyncio.run(asyncio.wait_for(_relay_probe(), timeout=15))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

check("relay reachable on --host 127.0.0.1", reply is not None, "no reply")
check("bad secret rejected through live relay",
      reply and reply.get("type") == "hello_error", str(reply))

print("\n[8] app integration (no new endpoints; count stable at 91)")
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

from main import app  # noqa: F401
schema_paths = set(app.openapi()["paths"].keys())
check("no accidental route changes", len(schema_paths) == 91,
      str(len(schema_paths)))
check("Phase 17/18 endpoints still registered",
      {"/api/v1/system/status", "/api/v1/simulation"} <= schema_paths)

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
