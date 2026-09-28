"""
SENTRA — Phase 20 hardware-independent tests.

Run:
    PYTHONIOENCODING=utf-8 python tests/test_phase20_hardening.py

Covers: core/hardening.py startup security checks (default JWT secret, auth
off, relay default secret / cleartext, clean case, summary shape),
core/sd_notify.py (no-op off-systemd + REAL AF_UNIX socket server + watchdog
pings), deploy unit watchdog upgrade, /system/status integration, and route
stability (91 paths).
"""

import asyncio
import importlib
import json
import os
import socket
import sys
import tempfile
import threading

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

import core.hardening as hard
import core.sd_notify as sdn

print("\n[2] hardening checks — default JWT secret is active on this dev box")
for var in ("JWT_SECRET_KEY", "SENTRA_RELAY_URL", "SENTRA_RELAY_UNIT_SECRET"):
    os.environ.pop(var, None)
findings = hard.run_checks()
ids = [f["id"] for f in findings]
check("JWT_DEFAULT_SECRET detected (shipped default in effect)",
      "JWT_DEFAULT_SECRET" in ids, str(ids))
check("auth-enabled → no AUTH_DISABLED finding", "AUTH_DISABLED" not in ids)
check("relay off → no relay findings",
      "RELAY_DEFAULT_SECRET" not in ids and "RELAY_CLEARTEXT" not in ids)
check("finding shape", all(set(f) == {"id", "severity", "detail"} and f["severity"] == "WARNING"
                           for f in findings), str(findings))

print("\n[3] clean case — real secrets, auth on, relay off")
os.environ["JWT_SECRET_KEY"] = "a" * 64
findings = hard.run_checks()
check("no findings with real secret", findings == [], str(findings))
s = hard.summary()
check("summary clean shape", s["clean"] is True and s["count"] == 0
      and s["checked_at_startup"] is True and s["findings"] == [], str(s))

print("\n[4] relay findings (default secret + cleartext)")
os.environ["SENTRA_RELAY_URL"] = "ws://relay.example:8765"
os.environ.pop("SENTRA_RELAY_UNIT_SECRET", None)
findings = hard.run_checks()
ids = [f["id"] for f in findings]
check("RELAY_DEFAULT_SECRET detected", "RELAY_DEFAULT_SECRET" in ids, str(ids))
check("RELAY_CLEARTEXT detected (ws://)", "RELAY_CLEARTEXT" in ids, str(ids))
os.environ["SENTRA_RELAY_UNIT_SECRET"] = "b" * 32
findings = hard.run_checks()
ids = [f["id"] for f in findings]
check("custom relay secret clears secret finding",
      "RELAY_DEFAULT_SECRET" not in ids and "RELAY_CLEARTEXT" in ids, str(ids))
os.environ["SENTRA_RELAY_URL"] = "wss://relay.example"
findings = hard.run_checks()
ids = [f["id"] for f in findings]
check("wss:// clears cleartext finding",
      "RELAY_CLEARTEXT" not in ids and "RELAY_DEFAULT_SECRET" not in ids, str(ids))

print("\n[5] auth-off finding + run_checks never raises")
os.environ["SENTRA_AUTH_ENFORCED"] = "false"
findings = hard.run_checks()
check("AUTH_DISABLED detected", any(f["id"] == "AUTH_DISABLED" for f in findings))
os.environ["SENTRA_AUTH_ENFORCED"] = "true"
check("checks re-run clean", hard.run_checks() == [])

print("\n[6] sd_notify — off-systemd no-op")
os.environ.pop("NOTIFY_SOCKET", None)
os.environ.pop("WATCHDOG_USEC", None)
check("notify() returns False off-systemd", sdn.notify("READY=1") is False)
check("heartbeat.start() returns False without WatchdogSec",
      sdn.heartbeat.start() is False)
check("heartbeat not running", sdn.heartbeat.running is False)
sdn.heartbeat.stop()  # idempotent no-op
check("stop() idempotent", True)

print("\n[7] sd_notify — watchdog pings (injected socket, cross-platform)")
import time


class _FakeSock:
    def __init__(self):
        self.sent: list[bytes] = []
        self._fail = False

    def sendall(self, data):
        if self._fail:
            raise OSError("injected failure")
        self.sent.append(data)


fake = _FakeSock()
sdn._notify_sock = fake  # inject: _connect() short-circuits to this
os.environ["WATCHDOG_USEC"] = "1000000"  # 1 s → ping every 0.5 s
try:
    check("notify(READY=1) delivered via socket", sdn.notify("READY=1") is True)
    check("READY=1 content", fake.sent and fake.sent[-1] == b"READY=1",
          str(fake.sent))
    check("heartbeat starts with WatchdogSec", sdn.heartbeat.start() is True)
    time.sleep(1.3)  # expect ~2 pings
    check("watchdog pings received",
          any(b"WATCHDOG=1" in s for s in fake.sent), str(fake.sent))
    sdn.heartbeat.stop()
    n = len(fake.sent)
    time.sleep(1.2)
    check("pings stop after stop()", len(fake.sent) == n,
          f"{n} → {len(fake.sent)}")
    fake._fail = True
    check("send failure degrades to False (no raise)",
          sdn.notify("READY=1") is False)
    fake._fail = False
finally:
    sdn.heartbeat.stop()
    sdn._notify_sock = None  # restore real _connect() path
    os.environ.pop("WATCHDOG_USEC", None)

if hasattr(socket, "AF_UNIX"):
    print("\n[7b] sd_notify — REAL AF_UNIX socket (Linux/Pi only)")
    sock_path = os.path.join(tempfile.mkdtemp(), "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    received: list[bytes] = []
    _done = threading.Event()

    def _reader():
        srv.settimeout(0.2)
        while not _done.is_set():
            try:
                received.append(srv.recv(4096))
            except socket.timeout:
                continue
            except OSError:
                break

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    os.environ["NOTIFY_SOCKET"] = sock_path
    try:
        check("real socket: READY=1 delivered", sdn.notify("READY=1") is True)
        check("real socket: content", received and b"READY=1" in received[-1],
              str(received))
    finally:
        _done.set()
        t.join(timeout=1)
        srv.close()
        os.environ.pop("NOTIFY_SOCKET", None)
        sdn._notify_sock = None
else:
    print("\n[7b] real AF_UNIX socket test skipped (Windows dev; covered on Pi)")

print("\n[8] deploy unit watchdog upgrade")
with open(os.path.join("deploy", "sentra-backend.service"), encoding="utf-8") as f:
    unit = f.read()
check("Type=notify", "Type=notify" in unit)
check("WatchdogSec=30s", "WatchdogSec=30s" in unit)
check("TimeoutStartSec set", "TimeoutStartSec" in unit)

print("\n[9] /system/status integration + routes stable")
os.environ.pop("JWT_SECRET_KEY", None)  # restore shipped default → expect a finding
import base64 as _b64
import services.relay_client as rc
from main import app  # noqa: F401

r = asyncio.run(rc.dispatch_local("GET", "/api/v1/system/status", None, {}, b""))
body = json.loads(_b64.b64decode(r["body_b64"]))
check("status 200", r["status"] == 200)
check("security block present", isinstance(body.get("security"), dict),
      str(body.get("security")))
sec = body.get("security", {})
check("security summary shape", {"checked_at_startup", "findings", "count", "clean"}
      <= set(sec), str(sec))
check("sim box JWT default detected in live snapshot",
      sec.get("clean") is False and sec.get("count", 0) >= 1, str(sec))
check("simulation block still present", "simulation" in body)
schema_paths = set(app.openapi()["paths"].keys())
check("route count >= 121 (Phase 21 owns exact count)",
      len(schema_paths) >= 121, str(len(schema_paths)))

# ── summary ──────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print(f"RESULT: {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}")
sys.exit(1 if FAIL else 0)
