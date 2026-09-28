# SENTRA Backend — Architecture Document (Phase 0 Audit)

> **PHASE 1 UPDATE (2026-09-27):** See [§17 Phase 1 Changes Applied](#17-phase-1-changes-applied) at the end of this document.

> Audit date: 2026-09-27 · Branch: `main` · Audited by reading every file in
> `~/sentra_backend` (30 Python files, `config.py`, `requirements.txt`, `.env.example`, `APIS.md`).
> **No functionality was modified during this phase.**

---

## 1. Current Architecture Overview

Single **FastAPI** app (`main.py`) run with **uvicorn** on the Pi (port 8080).
Everything lives in one process; background work runs in daemon threads.

```
Flutter app (LAN)
   ├── REST  ─────────────► routers/ ──► services/ ──► hardware (lgpio, OpenCV)
   ├── WebSocket ─────────► ws_handlers/ ─► services/
   ├── MJPEG  ────────────► routers/camera.py (Picamera2/OpenCV)
   └── UDP broadcast 8888 ─► services/udp_discovery.py  (SENTRA_DISCOVER → ACK)
```

Startup sequence (`lifespan` in `main.py`):
1. Detect LAN IP (`_get_local_ip`, UDP trick to `8.8.8.8`).
2. Start UDP discovery responder (port 8888).
3. `ultrasonic_service.start_monitoring()` — starts GPIO + a 5 Hz daemon thread.
4. Create snapshot dir, mount `/snapshots` static files.

### Module map

| Layer | Files | Role |
|---|---|---|
| Entry | `main.py` | App factory, CORS, lifespan, router mounting |
| Config | `config.py` | Pydantic `Settings` (env + `.env`): unit identity, network, JWT, motor/camera/IR defaults |
| Hardware | `harware/motor_driver.py`, `harware/ultrasonic_scan.py` | **Standalone ROS2 (rclpy) nodes — not used by FastAPI app** |
| Services | `motor_service`, `ultrasonic_service`, `telemetry_service`, `camera_service`, `call_service`, `udp_discovery` | Business logic + GPIO + simulated state |
| Routers | `system, auth, pair, telemetry, camera, control, alerts, settings, call` | REST under `/api/v1/...` |
| WS handlers | `telemetry_ws, control_ws, alerts_ws, call_ws` | `/ws/telemetry`, `/ws/control`, `/ws/alerts`, `/ws/webrtc/{role}`, `/ws/call/{role}` |
| Models | `models/requests.py`, `models/responses.py` | Pydantic request/response schemas |

**Dependency stack:** `fastapi 0.115.0`, `uvicorn[standard]`, `websockets`, `python-jose`, `passlib`, `opencv-python-headless 4.10`, `python-multipart`, `aiofiles`, `pydantic-settings`.
Note: `lgpio` is **not** in `requirements.txt` — it is imported with try/except (dev-mode fallback when absent). `psutil`/`Pillow` (used by `call_service`) are also not pinned.

---

## 2. Existing REST APIs

Base path `/api/v1`. Full historical spec in `APIS.md` (Flutter contract — must not break).

| Endpoint | Method | Handler | Notes |
|---|---|---|---|
| `/api/v1/ping` | GET | `routers/system.py` | Health check |
| `/api/v1/system/info` | GET | `routers/system.py` | Unit identity, firmware |
| `/api/v1/system/capabilities` | GET | `routers/system.py` | **Returns hardcoded values incl. `lidar_installed: true` (no LiDAR exists)** |
| `/api/v1/system/reboot` | POST | `routers/system.py` | Schedules `sudo reboot` after 3 s |
| `/api/v1/auth/session` | POST | `routers/auth.py` | JWT for roles OWNER/GUARD/GUEST |
| `/api/v1/pair` | POST | `routers/pair.py` | In-memory pairing registry, returns WS/stream URLs |
| `/api/v1/telemetry/initial-sync` | GET | `routers/telemetry.py` | Hardware/battery sync |
| `/api/v1/telemetry/live` | GET | `routers/telemetry.py` | Dashboard snapshot |
| `/api/v1/camera/stream.mjpg` | GET | `routers/camera.py` | MJPEG stream (Picamera2 → OpenCV → synthetic fallback) |
| `/api/v1/camera/ir-filter` | POST | `routers/camera.py` | IR toggle — **state-only, no real relay wired** |
| `/api/v1/camera/snapshot` | POST | `routers/camera.py` | JPEG to `~/sentra_snapshots`, served at `/snapshots` |
| `/api/v1/robot/mode` | POST | `routers/control.py` | `PATROL` \| `MANUAL` \| `STANDBY` |
| `/api/v1/robot/estop` | POST | `routers/control.py` | E-stop latch (kills PWM, sets disabled) |
| `/api/v1/robot/estop/reset` | POST | `routers/control.py` | Clears latch |
| `/api/v1/robot/speed` | POST | `routers/control.py` | `speed_multiplier` 0–2, `target_mps` 0–1.2 |
| `/api/v1/alerts` | GET | `routers/alerts.py` | In-memory list + 2 seeded demo alerts |
| `/api/v1/alerts/{id}/ack` | POST | `routers/alerts.py` | Acknowledge |
| `/api/v1/settings` | GET/PUT | `routers/settings.py` | In-memory dict (incl. fake `high_precision_lidar` flag) |
| `/api/v1/call/ping` | GET | `routers/call.py` | Video-call health |
| `/api/v1/call/upload-node` / `upload-user` | POST | `routers/call.py` | Raw JPEG frame push (HTTP fallback transport) |
| `/api/v1/call/node-stream.mjpg` / `peer-stream.mjpg` | GET | `routers/call.py` | MJPEG loops over shared frame buffers |

---

## 3. Existing WebSockets

| Socket | Direction | Protocol |
|---|---|---|
| `/ws/telemetry` | server → client @10 Hz | JSON payload: battery, latency, uptime, speed, pos_x/y, mode, ultrasonic, cliff, current_ma, mpu6050 |
| `/ws/control` | client → server @20 Hz | `{linear_velocity, angular_velocity, direction}` → `motor_service.apply_locomotion`, ACK per message. On disconnect → STOP |
| `/ws/alerts` | server → client | `broadcast_alert()` push + 30 s heartbeat. **Nobody currently calls `broadcast_alert()`** |
| `/ws/webrtc/{role}` | bidirectional | WebRTC signaling relay for roles `node`/`user` (ready/offer/answer/candidate/bye), single peer per role |
| `/ws/call/{role}` | bidirectional | Binary JPEG frame relay between node phone and user phone (MJPEG-over-WS video call) |

Video call has two transports: **WebSocket frame relay** (`/ws/call/{role}`) and **HTTP frame upload + MJPEG pull** (`/api/v1/call/upload-*` → `/api/v1/call/*-stream.mjpg`), plus WebRTC signaling. Both are functional.

---

## 4. Motor GPIO Mapping (as implemented)

`services/motor_service.py` is the live driver (L298N, `lgpio`, chip `gpiochip_open(4)` → Pi 5, PWM 1000 Hz, duty 0–100):

| Signal | GPIO |
|---|---|
| ENA (left PWM) | 12 |
| IN1 | 27 |
| IN2 | 17 |
| ENB (right PWM) | 13 |
| IN3 | 22 |
| IN4 | 23 |

> The user-specified mapping says IN1=27, IN2=17; code sets `ENA, IN1, IN2 = 12, 27, 17` — motor A is deliberately flipped to drive forward (commit "Motor direction problem solved"). Keep as-is.

Differential mixing: `left = linear − angular`, `right = linear + angular`, clamped ±1, scaled by `speed_multiplier`.
If `lgpio` is unavailable, `_h is None` → all commands silently no-op (dev mode).

**Dead code:** `harware/motor_driver.py` and `harware/ultrasonic_scan.py` are standalone **ROS2 (rclpy) nodes** publishing `/cmd_vel` and `/scan`. They duplicate the same GPIO pins. No ROS is used anywhere else; these are leftovers from an earlier approach.

---

## 5. Ultrasonic GPIO Mapping (as implemented)

`services/ultrasonic_service.py` reuses the motor service's lgpio chip handle (`_h` shared via import — fragile but working):

| Signal | GPIO |
|---|---|
| FRONT_TRIG | 24 |
| FRONT_ECHO | 25 |
| REAR_TRIG | 5 |
| REAR_ECHO | 6 |

Blocking busy-wait pulse timing (max 30 ms per phase), 0.05 s between front/rear, 0.2 s loop (5 Hz), results written into `telemetry_service._sim["ultrasonic"]`. Runs in a daemon thread — OK for the event loop, but Python busy-wait + `time.sleep` timing is drift-prone under load.

IR cliff sensors, MPU6050, wheel encoders: **declared in telemetry payload but not wired to any hardware** — values are static placeholders in `_sim`.

---

## 6. Camera Implementation

`services/camera_service.py`:
- Priority: Picamera2 (1280×720 video config) → OpenCV `VideoCapture(0)` → synthetic grey frame with timestamp (dev).
- `_get_global_camera()` lazy-init with thread lock; generator holds the lock each frame; ~30 FPS via `time.sleep(0.03)` in the generator (blocking, per-client).
- Snapshot endpoint opens a **new** camera instance each call (`_try_picamera2()` again) — can conflict with the stream camera on Pi.
- IR filter toggle is a boolean flag only; no GPIO relay exists.

Design gap vs target architecture: the phone camera is the intended primary vision source, but the current pipeline only serves Pi-side camera capture. There is no frame path from the mounted phone into a *vision processing* service (the call service relays frames for the call feature only, discarding them after broadcast).

---

## 7. Authentication Implementation

- `POST /api/v1/auth/session` → `python-jose` HS256 JWT, payload `{sub: device_id, role, permissions, exp}`, roles OWNER/GUARD/GUEST with a static permission map.
- **No middleware/dependency enforces the token on any endpoint** — every router is publicly callable on the LAN. JWT is minted but never verified anywhere.
- `POST /api/v1/pair` accepts any `app_instance_id` + `ip_address`, stores in an in-memory dict that is never consulted again.
- WS endpoints have no auth at all; `jwt_secret_key` default is committed to the repo.
- No device-role concept yet (rover-mounted phone vs user phone).

---

## 8. Video Call Implementation

Roles: `node` = phone mounted on rover, `user` = remote viewer phone.
- Shared in-memory JPEG buffers (`node_frame`, `user_frame`), 5 s staleness window, EXIF-orientation-aware downscale to 640 px via Pillow.
- Binary frame relay over `/ws/call/{role}` with per-role connection sets; latest peer frame replayed on connect.
- WebRTC signaling relay in `/ws/webrtc/{role}` (SDP/candidate size limits, peer replacement on reconnect, `peer-ready`/`peer-left` events).
- Placeholder frames generated with Pillow while a side is absent.
- Quality: JPEG q55 for HTTP uploads; WS frames passed through raw.

No call lifecycle API yet (`/call/start`, `/call/end`, `/call/status`) and no link to alerts/emergency events.

---

## 9. Existing Patrol Implementation

Currently a **bump-and-wander loop, not a real patrol**:
`motor_service._patrol_loop()` daemon thread @2 Hz:
- Reads distances from `telemetry_service._sim["ultrasonic"]`.
- front<1.0 and rear<1.0 → STOP; front<1.0 → BACKWARD (−0.5); else FORWARD (+0.5).
- Guarded by `mode == "PATROL"` and `estop_active`; runs even when lgpio is missing (no-op in dev).

Issues: started unconditionally at import time (module import has side effects — thread starts even in tests), reads simulated sensor dict rather than real sensor service state, no waypoints, no AprilTags, no location awareness, no route persistence, bypasses any notion of a safety layer (it *is* the only "safety"), and its BACKWARD reaction can back into the rear obstacle.

---

## 10. Existing E-stop Implementation

- `motor_service.trigger_estop()`: latches `estop_active=True`, `motors_disabled=True`, zeros both motors, logs CRITICAL.
- `reset_estop()` clears the latch (no mode/state reset beyond that).
- `apply_locomotion()` checks the latch first and refuses new commands while latched.
- Exposed via `POST /api/v1/robot/estop` and `/estop/reset`.
- WS control handler stops motors on client disconnect.

Missing: no hardware watchdog (PWM keeps its last duty if the process crashes — L298N has no enable line wired to a kill relay), no command timeout watchdog, no obstacle-driven stop outside the patrol thread, no E-stop broadcast to alert WS clients.

---

## 11. Telemetry & State

- `_sim` dict in `telemetry_service.py` is the de-facto **global robot state**: battery, latency, patrol speed, position, ultrasonic, cliff, current, IMU. Real sensors partially overwrite it (ultrasonic loop); the rest is static demo data.
- Two telemetry payloads: REST `/live` (dashboard snapshot, hardcoded "ARMED"/zone strings) and WS 10 Hz payload (sensor-centric).
- Mode truth lives in `motor_service._state["mode"]`; telemetry WS reports hardcoded `"mode": "PATROL"` — can contradict actual mode.

---

## 12. Known Problems (ordered by severity)

1. **No enforced auth anywhere.** JWT exists but is never checked; motors controllable by any LAN client. WS control has no auth either.
2. **No centralized safety layer.** Only E-stop latch + ad-hoc obstacle checks inside the patrol thread. No obstacle stop in manual mode, no command timeout, no cliff checks, no max-speed enforcement beyond the multiplier.
3. **Module-import side effects.** `motor_service` starts its patrol thread at import; `ultrasonic_service` opens GPIO at `start_monitoring()`; tests/dev on non-Pi machines rely on silent no-ops. Any future refactor must not multiply these hazards.
4. **Blocking calls in async paths.** MJPEG generator does camera capture + `time.sleep` per request thread; ultrasonic loop busy-waits; `_read_cpu_temp` uses subprocess — none crash the loop, but they limit scalability (e.g., two simultaneous stream clients contend on `_cam_lock`).
5. **Duplicate/contradictory sources of truth.** `_sim` vs `motor_service._state` vs hardcoded strings (`mode: PATROL` in WS payload, capabilities endpoint, seeded alerts, fake LiDAR settings flags).
6. **Dead ROS2 code** in `harware/` duplicating motor/ultrasonic pins (different IN1/IN2 order! `12,17,27` vs live `12,27,17` — confusing trap).
7. **Camera resource contention.** Snapshot path re-initializes a second Picamera2 while the stream may hold the first; `_cam_lock` held during entire capture loop iteration.
8. **Patrol loop reads simulated values** and reacts by backing up blindly; runs even when sensors are absent; 1.0 m threshold hardcoded, not configurable.
9. **In-memory persistence only.** Alerts, settings, pairing registry reset on restart. No SQLite/JSON persistence anywhere.
10. **Ultrasonic shared-handle coupling.** `ultrasonic_service` imports motor service's private `_h`; shutdown order (`atexit` closes chip) could race the monitoring thread.
11. **requirements.txt incomplete.** Missing `lgpio` (Pi-only, acceptable), `psutil`, `Pillow`, and pins assume x86 builds of opencv-headless work on aarch64 (they do, but Picamera2 comes from system packages).
12. **Snapshot/alert files accumulate** in `~/sentra_snapshots` with no cleanup; snapshots dir also doubles as static mount root.
13. **`/ws/control` accepts any payload shape silently** (direction string unvalidated, e.g. "STOP" check happens only after scaling).
14. **`telemetry_service.get_live_telemetry`** hardcodes "Active Perimeter Patrol"/zone regardless of reality.

## 13. Duplicate Functionality

- Motor + ultrasonic logic exists twice (live `services/*` vs dead ROS `harware/*`).
- Two MJPEG streaming systems: Pi camera stream (`camera_service`) and call relay streams (`call_service`) — different boundary conventions (`frame` vs `frame`/`frame`), different quality paths.
- Two camera-frame ingress transports for calls (WS binary and HTTP upload).
- Two alert-ish channels: REST alert store and `alerts_ws.broadcast_alert` (unused).
- Three "state" stores: `_sim`, `_state`, `_paired_apps`/`_settings`/`_alerts` dicts.

## 14. Missing Functionality (mapped to phases)

| Phase target | Status today |
|---|---|
| Central robot state machine (MANUAL/PATROL/NAV/RETURN_TO_DOCK/…) | ✅ Phase 1 (mode arbitration + owners) |
| Safety service (obstacle/timeout/cliff/mode separation) | ✅ Phases 1-3 (core/safety layer) |
| Unified sensor service (front/rear/cliff/IMU/encoders) | ✅ Phase 4 (health per sensor) |
| AprilTag localization service + tag map | ✅ Phase 5 |
| Manual mapping APIs (`/map/*`) | ✅ Phase 6 |
| Patrol routes / waypoints / `/patrol/*` APIs | ✅ Phase 7 (engine + persistence) |
| Dock / return-to-dock | ✅ Phase 8 |
| Voice command intake (`/voice/command`) | ✅ Phase 9 |
| Person detection / recognition | ✅ Phases 10-11 |
| Fall detection + temporal confirmation | ✅ Phase 12 |
| Automatic call on fall + call lifecycle APIs | ✅ Phase 13 |
| Notification service (WS push of real events) | ✅ Phase 14 (persistent + push) |
| Device roles (rover phone vs user phone) + enforced auth | ✅ Phase 15 (JWT + registry + middleware) |
| Remote access / cloud relay design | ✅ Phase 16 (tunnel + DANGER push) |
| `/system/status` with real CPU/RAM/temp/disk | ✅ Phase 17 (psutil, real metrics) |
| Simulation mode (`SENTRA_SIMULATION`) | ✅ Phase 18 (explicit mode: GPIO/I2C refused, preflight stop, banner) |
| systemd service | ✅ Phase 19 (hardened units + installer + env template) |
| Test suite | ✅ Phases 1-20 (20 suites, 681 tests, all green) |
| Startup hardening / watchdog | ✅ Phase 20 (security checks + sd_notify watchdog) |

## 15. What Must Be Preserved (frontend contract)

Per `APIS.md`, the Flutter app depends on: ping/system info/capabilities shapes, auth session, pair response URLs, telemetry REST+WS payload keys, MJPEG stream at `/api/v1/camera/stream.mjpg`, ir-filter, snapshot, robot mode/estop/speed, alerts GET/ack, settings GET/PUT, `/ws/control` message format, call WS + upload/stream endpoints, UDP discovery string `SENTRA_DISCOVER` → `SENTRA_ACK:<ip>:<port>:<unit>`.
Phase 1+ refactors must keep these routes and payload keys working (additive fields allowed; renames must be coordinated).

## 16. Key Decisions Locked In (from Phase 0 review)

| Decision | Choice |
|---|---|
| Endpoint compatibility | Keep existing routes working; centralize payload mapping so the Flutter rebuild is easy. Changing an endpoint payload = edit one adapter function, not scattered handlers. |
| Dead ROS code (`harware/`) | Moved to `not_needed/harware/` (not deleted). |
| Auth timing | **No enforcement during feature phases.** JWT path prepared (dependency stub) but permissive until features are complete and tested. |
| Vision source | **Mobile phone camera ONLY.** No Pi camera, no USB webcam. All vision (AprilTag, person, fall) consumes phone-camera frames relayed to the backend. `camera_service` MJPEG remains for backward compatibility but is not the vision source. |

### Changing a Flutter-facing payload (workflow)

The Flutter app will be rebuilt with the backend mapping features. To keep that
smooth, payload construction is centralized:

- REST payloads → builder functions in `services/telemetry_service.py` and typed models in `models/responses.py`
- WS payloads → `services/telemetry_service.get_ws_telemetry_payload()`
- When you need to change a payload for the new Flutter app, edit the builder/model in ONE place; old fields can be kept and new fields added side-by-side during the transition.

## 17. Phase 1 Changes Applied

### New files

| File | Purpose |
|---|---|
| `core/__init__.py` | Core layer package (state / safety / config) |
| `core/state.py` | Central thread-safe robot state machine. Modes: MANUAL, PATROL, NAVIGATION, FOLLOW_PERSON, RETURN_TO_DOCK, EMERGENCY_STOP, STANDBY. Mode ownership arbitration; e-stop latch + reset (reset always lands in STANDBY, never auto-resumes motion). |
| `core/config.py` | Runtime-tunable behavior config via env vars: `SENTRA_SIMULATION`, obstacle stop distances, command timeouts, patrol tick, speed ceiling. `VISION_SOURCE = mobile_phone_camera`. |
| `core/safety.py` | **The centralized safety layer.** Every motor command passes `SafetyLayer.check_and_apply(owner, mode, left_pct, right_pct)`. Enforces: e-stop → disabled latch → mode/owner match → command-timeout watchdog (0.2 s tick thread) → cliff/front/rear obstacle STOP → speed clamp → NaN/inf rejection. Motor driver injected as a callable (hardware-independent, unit-tested). `force_stop()` always allowed. |
| `tests/test_phase1_core.py` | 41 hardware-independent tests (state machine, safety gating, motor differential math, e-stop dominance, timeouts, obstacle stops, app import integrity). |
| `not_needed/harware/` | Archived dead ROS2 nodes (was `harware/`). |

### Changed files

| File | Change |
|---|---|
| `services/motor_service.py` | Refactored: all movement now goes through `core.safety` via `apply_wheel_speeds(left, right, owner, mode)`. **Patrol thread no longer starts at import** — explicit `start_patrol_loop()/stop_patrol_loop()` called from lifespan. Patrol now STOPS at obstacle (was: back up blindly). Legacy `apply_locomotion()` signature unchanged for `/ws/control`; auto-adopts MANUAL from STANDBY for app compatibility. Public REST-facing functions (`trigger_estop`, `reset_estop`, `set_mode`, `set_speed`, `get_state`) keep identical return shapes. |
| `main.py` | Lifespan wires safety layer (`motor_apply` + sensor provider), starts watchdog, starts/stops patrol thread explicitly, and calls `stop_all("shutdown")` on exit. |
| `services/telemetry_service.py` | WS telemetry now reports the REAL central mode from `core.state` (was hardcoded "PATROL"). |
| `services/__init__.py`, `models/__init__.py` | Documented Phase 1 service/model map for upcoming phases. |

### Behavior changes to be aware of

1. **Patrol obstacle handling:** previously backed up blindly on front obstacle; now STOPS (per safety rule) — smarter avoidance comes with Phase 7 routes.
2. **E-stop reset → STANDBY**, not previous mode. Client must re-select PATROL/MANUAL. This is intentional.
3. **Manual streaming commands** now have a 0.6 s timeout (env: `SENTRA_CMD_TIMEOUT_S`): if the app stops sending commands, motors stop. The app already streams at 20 Hz so this is transparent — but a paused joystick now also means stop, which is safer.
4. **Speed multiplier** is clamped to ≤ 1.0 by `set_speed` (was ≤ 2.0 accepted).
5. Only one movement mode can own the motors at a time; a patrol loop and a manual joystick cannot fight.

### APIs added

None — Phase 1 is internal architecture. All 23 REST paths and 5 WS endpoints unchanged and verified.

### Verification performed

- `PYTHONIOENCODING=utf-8 python tests/test_phase1_core.py` → 41 passed, 0 failed
- Live boot: all 23 REST paths present; mode transitions logged; WS telemetry propagated real mode (PATROL) within one 10 Hz tick; control WS ACK path works; estop/estop-reset/speed responses unchanged.

---

## 18. Phase 2 — Manual Motor Control (2026-09-27)

### New files

| File | Purpose |
|---|---|
| `services/motion_controller.py` | **The single MANUAL-mode authority.** 20 Hz ramp loop; acceleration (150 %/s) and deceleration (300 %/s) limiting; wheel-speed + directional + differential targets; one-shot duration expiry; source-silence auto-stop; brake sequencing. Feeds every command through `core.safety`. |
| `routers/manual_control.py` | REST: `POST /api/v1/control/move` (wheel or directional, one-shot), `/stop`, `/brake`, `/estop`, `/estop/reset`, `GET /state`. |
| `models/control.py` | `MoveRequest`, `StopRequest`, `BrakeRequest`, `ControlActionResponse`, `ControlStateResponse`. |
| `tests/test_phase2_motion.py` | 45 hardware-independent tests. |

### Changed files

| File | Change |
|---|---|
| `ws_handlers/control_ws.py` | Now routes through the motion controller. Accepts legacy linear/angular form (unchanged behavior) PLUS new forms: `{"type":"wheel","left":50,"right":50}`, `{"type":"direction","direction":"FORWARD","scale":0.5}`, `{"type":"stop"}`, `{"type":"brake"}`, `{"left_speed":..,"right_speed":..}`. Adds independent 0.5 s `{"type":"state",...}` push. Legacy ACK shape preserved. |
| `services/motor_service.py` | Added `apply_active_brake()` (L298N shorted-terminals brake pulse, GPIO-lock protected) and `_gpio_lock` for thread-safe lgpio access. |
| `core/config.py` | Added motion tuning: `ACCEL_PCT_PER_S` (150), `DECEL_PCT_PER_S` (300), `MOTION_TICK_S` (0.05), `BRAKE_HOLD_S` (0.5), `MAX_MOVE_DURATION_S` (30) — all env-overridable. |
| `main.py` | Starts/shuts down motion controller in lifespan. |

### APIs added (Phase 2)

| Endpoint | Body | Response |
|---|---|---|
| `POST /api/v1/control/move` | `{"left_speed":50,"right_speed":50,"duration":2.0}` or `{"direction":"FORWARD","scale":0.5,"duration":1.5}` | `{applied, action, left, right}` |
| `POST /api/v1/control/stop` | `{}` | `{applied, action:"stop"}` |
| `POST /api/v1/control/brake` | `{}` | `{applied, action:"brake"}` |
| `POST /api/v1/control/estop` | `{}` | same as `/robot/estop` (shared latch) |
| `POST /api/v1/control/estop/reset` | `{}` | same as `/robot/estop/reset` |
| `GET /api/v1/control/state` | — | current/target duty, braking, mode, estop, moving, speed_multiplier |

### WebSocket changes (/ws/control)

New inbound forms (legacy stays valid) and a new outbound push:
```
← {"type":"state","current_left":..,"current_right":..,"target_left":..,
    "target_right":..,"braking":bool,"active":bool,"mode":..,
    "estop_active":bool,"moving":bool,"speed_multiplier":..}   (every 0.5 s)
```

### Verification performed (Phase 2)

- `PYTHONIOENCODING=utf-8 python tests/test_phase2_motion.py` → 45 passed, 0 failed (ramp math, all message forms, one-shot expiry, silence auto-stop, obstacle hold, e-stop dominance, brake, REST handlers, route registration)
- Phase 1 suite re-run → 41 passed, 0 failed (no regressions)
- Live server: 6 new REST endpoints respond; ramp loop observable via `/control/state` (`current_left` climbing toward `target_left`); WS legacy + wheel + direction + stop + brake ACKs and independent state push all verified.

### Known limitations (Phase 2)

1. Acceleration is duty-based (open loop) — true velocity ramping needs wheel-encoder feedback (Phase 4).
2. `BRAKE` holds motor terminals shorted for 0.5 s; frequent braking at high speed generates heat — tunable via `SENTRA_BRAKE_HOLD_S`.
3. REST `/move` is one-shot (max 30 s); continuous driving must use `/ws/control`.
4. Only the motion controller may drive in MANUAL; the old direct `apply_locomotion` path still exists for compatibility but now routes through the same gate.

---

## 19. Phase 3 — Safety Layer (2026-09-27)

### New files

| File | Purpose |
|---|---|
| `core/safety_config.py` | Runtime-configurable safety thresholds with hard bounds (`LIMITS`). Env-var startup values → identical behavior after upgrade. Batch-atomic updates (one bad key rejects the whole batch). Not yet persisted (env defaults on restart). |
| `services/cliff_service.py` | IR cliff sensor polling (10 Hz, daemon thread). Env-configured pins (`SENTRA_CLIFF_LEFT_GPIO`=19, `SENTRA_CLIFF_RIGHT_GPIO`=26, `SENTRA_CLIFF_ACTIVE_HIGH`, `SENTRA_CLIFF_ENABLED`). Simulated fallback on dev machines. Mirrors state into telemetry `_sim["cliff"]`. No import-time GPIO side effects. |
| `services/safety_events.py` | Debounced (2 s) safety event ring buffer (200 max) + counters. Bridges thread-world events to the async alerts WS via a queued pump task. Types: ESTOP, OBSTACLE, CLIFF, TIMEOUT, MODE_MISMATCH. |
| `routers/safety.py` + `models/safety.py` | REST: status, events, thresholds GET/PUT/limits/reset. |
| `tests/test_phase3_safety.py` | 39 tests. |

### Changed files

| File | Change |
|---|---|
| `core/safety.py` | Reads thresholds from runtime config (not static constants); every stop/timeout records a safety event via injected reporter (core stays service-free); `last_decision` tracked; new `status()` snapshot. |
| `services/motion_controller.py` | Ramp rates from runtime config. |
| `services/motor_service.py` | `set_speed` ceiling from runtime config; patrol obstacle threshold from runtime config. |
| `ws_handlers/alerts_ws.py` | **Fixed latent crash:** `_subscribers -= dead` rebound the module-level set name → `UnboundLocalError` on every broadcast. Now mutates in place with `difference_update`. This path was never exercised before Phase 3. |
| `main.py` | Wires event reporter, attaches asyncio pump, starts/stops cliff monitoring. |

### APIs added (Phase 3)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/safety/status` | gate status, last decision, live sensors, thresholds, watchdog state |
| `GET /api/v1/safety/events?limit=N` | recent events + per-type counters |
| `GET /api/v1/safety/thresholds` | current runtime thresholds |
| `GET /api/v1/safety/thresholds/limits` | hard min/max bounds per key |
| `PUT /api/v1/safety/thresholds` | update (validated, batch-atomic, bounds-enforced) |
| `POST /api/v1/safety/thresholds/reset` | restore env defaults |

### WebSocket events added

`/ws/alerts` now actually broadcasts (previously dead code):
```
{"type": "safety_event", "event": "OBSTACLE"|"CLIFF"|"TIMEOUT"|"ESTOP"|"MODE_MISMATCH",
 "severity": "WARNING"|"DANGER", "detail": {...}, "timestamp": ...}
```

### Verification performed (Phase 3)

- Phase 3 suite: 39 passed (bounds, atomic batches, dynamic thresholds, cliff toggle, runtime timeouts, debounce, REST handlers)
- Full regression: 41 + 45 + 39 = **125 passed, 0 failed**
- Live: PUT threshold 1.5 m → drive command blocked at simulated 1.2 m obstacle, motors held at 0, OBSTACLE event recorded; reset → same command succeeds; alerts WS client received the safety_event in real time.

### Known limitations (Phase 3)

1. Thresholds not persisted — reset to env defaults on restart (persistence phase will store them).
2. Cliff GPIO pins default to 19/26 — **verify against your actual IR module wiring** before trusting hardware reads; currently simulated everywhere.
3. Safety events live in memory (200-deep ring); use `GET /events` promptly or add persistence later.
4. ESTOP event fires on the gate path; e-stop via REST logs CRITICAL but only reports once (debounce) — intentional.

---

## 20. Phase 4 — Sensor Service (2026-09-27)

### New files

| File | Purpose |
|---|---|
| `services/imu_service.py` | MPU6050 over I2C (smbus2), 20 Hz background thread, complementary-filtered pitch/roll + gyro-integrated yaw, burst 14-byte reads, WHO_AM_I probe. Simulated (clean zeros) without hardware. Env: `SENTRA_IMU_BUS/ADDR/POLL_S/ENABLED`. |
| `services/encoder_service.py` | Wheel encoders via lgpio edge callbacks (ticks → speed + distance, `SENTRA_TICKS_PER_REV`/`SENTRA_WHEEL_DIAM_M`). **Pin-collision guard** refuses to claim motor/ultrasonic/cliff pins → runs duty-derived simulation instead (speed from commanded duty, ~0.6 m/s @ 100%). `reset_odometry()` for future phases. |
| `services/sensor_service.py` | Unified 10 Hz aggregator: one snapshot (distances, cliffs, IMU, encoders) + per-sensor health (FRESH/STALE/SIMULATED/DISABLED/NO_DATA, freshness limit 1.5 s). Provides the safety gate's sensor input. Never blocks the event loop (all hardware reads live in per-sensor threads). |
| `tests/test_phase4_sensors.py` | 33 tests. |

### Changed files

| File | Change |
|---|---|
| `main.py` | Safety gate now reads `sensor_service.sensor_provider()` (unified snapshot) instead of assembling fragments itself; IMU/encoder/aggregator lifecycle in lifespan. |
| `routers/telemetry.py` | New `GET /api/v1/telemetry/sensors` — snapshot + health. |
| `services/telemetry_service.py` | WS payload enriched additively: live IMU replaces static `mpu6050` placeholder, `wheel_encoders` object added; all legacy keys/shapes preserved. Ultrasonic gains internal `last_read` (stripped from WS payload). |
| `requirements.txt` | Added `smbus2`, `Pillow` (was already imported by call_service but undeclared). |

### APIs added (Phase 4)

`GET /api/v1/telemetry/sensors` → `{"sensors": {...}, "health": {"ultrasonic": {...}, "cliff": {...}, "imu": {...}, "wheel_encoders": {...}}}`

### WebSocket changes

`/ws/telemetry` payload: `mpu6050` is now live IMU data (pitch/roll/yaw/ax/ay/az/gx/gy/gz/temp_c — superset of the old pitch/roll/yaw), `wheel_encoders` added (ticks/speed_mps/distance_m). All v1 keys intact.

### Verification performed (Phase 4)

- Phase 4 suite: 33 passed; full regression 41+45+39+33 = **158 passed, 0 failed**
- Live: `/telemetry/sensors` returns health map; drive 1.5 s @ 60% duty → simulated odometry +0.505 m per wheel (physically plausible); WS payload carries live IMU + encoder data; test suite proved the command-timeout auto-stop still engages through the unified provider.

### Known limitations (Phase 4)

1. **Encoder pins are placeholders** (default `SENTRA_ENCODER_LEFT_GPIO=23` collides with IN4 → guard forces simulation). Set real pins via env vars before hardware use.
2. IMU yaw is gyro-integrated only — drifts without a magnetometer (fine for tilt; heading needs the AprilTag layer from Phase 5).
3. Encoder simulation models duty→speed linearly (0.6 m/s @ 100%); calibrate `SENTRA_WHEEL_DIAM_M` and real max speed on hardware.
4. Ultrasonic health reads from telemetry `_sim`; on hardware it will show FRESH once real reads stamp `last_read`.

---

## 21. Phase 5 — AprilTag Localization (2026-09-27)

### Vision architecture (per locked decision)

```
Phone camera (rover) ──JPEG──► call_service ingress (/ws/call/node or /upload-node)
                                     │
                                     ▼
                        vision_service sampler (10 Hz, drop-oldest)
                                     │
                 ┌───────────────────┼─────────────────┐
                 ▼                   ▼                 ▼
          apriltag_service    (person: Ph.10)   (fall: Ph.12)
                 │
                 ▼
        localization_service ──► tag_map (data/tag_map.json)
                 │
                 ▼
        Backend decision layer (patrol / navigation / voice in later phases)
```

The call feature is untouched — vision taps the same frames.

### New files

| File | Purpose |
|---|---|
| `services/tag_map.py` | Persistent configurable tag→location map (`data/tag_map.json`, atomic writes, thread-safe). Defaults (1=Dock/DOCK, 2=Kitchen, 3=Bedroom, 4=Hall, 5=Living Room) created on FIRST RUN only — everything editable via API. Case-insensitive name lookup ready for Phase 9 voice. |
| `services/apriltag_service.py` | 36h11 detector (OpenCV Aruco, cached detector instance). Returns tag_id, timestamp, distance (apparent-size model from `SENTRA_TAG_SIZE_M` + `SENTRA_CAM_HFOV_DEG`), bearing, confidence, corners. History + last-seen + freshness window. `inject_detection()` for simulation. |
| `services/vision_service.py` | Phone-frame ingress hub: samples latest node frame at 10 Hz from the call path, decodes, fans out to subscribers with bounded drop-oldest queues (vision can never slow the call). Person/fall services subscribe the same way later. |
| `services/localization_service.py` | Where am I: last-known location + age, visible described tags, DOCK-priority/closest/most-confident selection, `Unknown`-safe telemetry string. |
| `routers/localization.py`, `models/localization.py` | REST endpoints below. |
| `tests/test_phase5_apriltag.py` | 38 tests incl. REAL detection of a rendered 36h11 tag. |

### APIs added (Phase 5)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/localization/status` | located, last_known, visible_tags, detector stats |
| `GET /api/v1/localization/tags` | list the tag map |
| `PUT /api/v1/localization/tags/{tag_id}` | create/update binding `{name, type: LOCATION\|DOCK, notes}` |
| `DELETE /api/v1/localization/tags/{tag_id}` | remove binding |
| `POST /api/v1/localization/detect` | debug: POST JPEG body → detections (same detector as live path) |
| `GET /api/v1/localization/vision-stats` | ingress health (frames, subscribers, phone_streaming) |

### Verification performed (Phase 5)

- 38/38 Phase 5 tests (defaults, CRUD, persistence, rename, synthetic-tag real detection, distance/bearing sanity, no false positives, injection, localization selection, vision hub, routers, routes)
- Full regression: 41+45+39+33+38 = **196 passed, 0 failed**
- Live: defaults auto-created; real tag-2 JPEG detected (dist 0.117 m, conf 1.0); status showed `Kitchen`; rename to "Cooking Zone" reflected instantly in localization; `data/tag_map.json` persisted; vision sampler idle-safe with no phone streaming.

### Known limitations (Phase 5)

1. Distance is apparent-size based (needs `SENTRA_TAG_SIZE_M` and FOV set correctly); orientation (pose) requires calibrated intrinsics — fields are None until then.
2. Localization is tag-presence-based (semantic map, not SLAM) — no position between tags; odometry fusion arrives with Phase 6/7.
3. Detection runs only when the rover phone streams via the call path; `/camera/stream.mjpg` (Pi camera) is not a vision source by design.
4. Detection at 10 Hz on Pi 5 at 640px ≈ light CPU load; raise `SAMPLER_HZ` only after measuring on hardware.

---

## 22. Phase 6 — Manual Mapping (2026-09-27)

### Flow

```
POST /map/start → drive with joystick (MANUAL) → phone camera sees tag
   → vision hub → apriltag → mapping_service.capture (frame JPEG + IMU +
   encoder + ultrasonic context) → pending capture in GET /map
   → POST /map/tag {tag_id, name} → binding persisted in data/tag_map.json
   → POST /map/stop
```

Captures auto-expire (10 min TTL) if never named; named bindings persist.
Frame snapshots saved to `~/sentra_maps/map_<capture_id>.jpg`.

### New files

| File | Purpose |
|---|---|
| `services/mapping_service.py` | Session lifecycle (MANUAL auto-adopt, e-stop blocks), capture pipeline with per-tag dedup + seen_count, context snapshot via unified sensor service, naming → tag_map, TTL cleanup. Vision subscriber is order-safe (falls back to detecting in-frame if apriltag ran later). |
| `routers/mapping.py`, `models/mapping.py` | REST endpoints below. |
| `tests/test_phase6_mapping.py` | 34 tests. |

### APIs added (Phase 6)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/map/start` | begin session (auto-adopts MANUAL; refuses under e-stop) |
| `POST /api/v1/map/stop` | end session |
| `GET /api/v1/map` | overview: session, captures (named + pending), tag count |
| `POST /api/v1/map/tag` | `{tag_id, name, type, notes}` → name pending capture OR bind directly; persists |
| `DELETE /api/v1/map/tag/{tag_id}` | delete a binding from the persistent map (spec endpoint) |
| `DELETE /api/v1/map/captures/{capture_id}` | discard a pending capture + its frame file |
| `GET /api/v1/map/status` | service status (dir, TTL) |

### Verification performed (Phase 6)

- 34/34 tests (lifecycle, dedup, context, persistence, protection of named captures, TTL expiry, real-detection correlation via `on_frame`, routers, routes)
- Full regression: 41+45+39+33+38+34 = **230 passed, 0 failed**
- Live: start → session id; naming tag 2 as Kitchen persisted (tag map + localization status showed `Kitchen`); `DELETE /map/tag/3` removed binding; stop; 7 map paths registered.

### Known limitations (Phase 6)

1. Session/captures are in-memory (intentional — the durable artifact is the tag map); a crash mid-session loses unnamed captures only.
2. Frame snapshots accumulate in `~/sentra_maps` — cleanup job deferred.
3. Capture context reflects the latest aggregated sensor frame (±100 ms), not the exact camera instant.
4. The spec's `DELETE /map/tag/{id}` deletes bindings; capture-level deletion is separate (`/map/captures/{id}`).

---

## 23. Phase 7 — Patrol System (2026-09-27)

### Model

A **route** is an ordered list of tag-map location names (validated at save time), persisted to `~/sentra_data/patrol_routes.json` with a default route. A **session** walks the route: cruise FORWARD through the safety gate → confirm each waypoint by seeing its mapped tag within a 3 s freshness window → next. Obstacles/cliffs STOP the rover (never steer around); it waits and resumes on clear. Waypoint timeout (60 s) → skip with a PATROL event. Manual takeover (joystick or `set_mode`) ends the patrol; e-stop blocks starting.

Without any routes, `start_patrol()` falls back to the **Phase 1 legacy wander** (status reports `legacy_wander: true`), so pre-Phase-7 behaviour is preserved.

### New/changed files

| File | Purpose |
|---|---|
| `services/patrol_service.py` | Routes store (atomic JSON persistence, tag-map validation) + 5 Hz route engine (confirm / block / timeout / complete / takeover) + session API + legacy fallback. |
| `routers/patrol.py`, `models/patrol.py` | REST endpoints below. |
| `services/motor_service.py` | `set_mode(PATROL)` delegates to the patrol service (route session when routes exist); `set_mode(other)` ends any patrol; legacy wander loop defers while a route session is active (no driver fights). |
| `services/motion_controller.py` | Joystick takeover ends the patrol session (route + legacy). |
| `core/safety.py` | `read_sensors()` public accessor; mode-mismatch now zeroes motors (clean takeover — no stale PWM). |
| `services/safety_events.py` | New `PATROL` event type → `/ws/alerts` push. |
| `core/config.py` | `SENTRA_PATROL_CRUISE_DUTY` (40), `SENTRA_PATROL_WP_TIMEOUT_S` (60), confirm freshness (3 s), block poll (0.5 s), routes path. |
| `tests/test_phase7_patrol.py` | 39 tests. |

### APIs added (Phase 7)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/patrol/start` | `{route?}` — start on named/default route; legacy wander if none |
| `POST /api/v1/patrol/stop` | stop patrol |
| `GET /api/v1/patrol/status` | session progress, current waypoint, blocked state, mode |
| `POST /api/v1/patrol/routes` | `{name, waypoints[], set_default}` — validated vs tag map |
| `GET /api/v1/patrol/routes` | list routes + default + storage path |
| `POST /api/v1/patrol/routes/default` | switch default route |
| `DELETE /api/v1/patrol/routes/{name}` | delete route |

### WebSocket events added

`/ws/alerts` `safety_event` with `event: "PATROL"` and details `{event: started\|waypoint\|blocked\|completed\|stopped, route, waypoint?, reason?}`.

### Verification performed (Phase 7)

- 39/39 tests (store validation + persistence, Dock confirm → index advance, obstacle block → resume, completion → STANDBY, timeout skip, joystick takeover, REST takeover, legacy fallback, e-stop block, routers, routes)
- Full regression: **269 passed, 0 failed** across all 7 suites
- Live: route created/default, unknown waypoint rejected with clear error, patrol started (0/3 at Dock), cruising status, manual drive ended patrol (mode MANUAL), routes listed with storage path.

### Known limitations (Phase 7)

1. Waypoint confirmation = tag visibility; between tags the rover drives blind-forward (odometry-based drift correction comes with Phase 8/9 tuning).
2. Route loops (ending where it started) are allowed but each waypoint is visited once per session.
3. Blocked state has no telemetry push yet (visible via `/patrol/status` and the one-shot PATROL event).
4. On the Pi, place real tags; confirmation depends on the phone camera actually seeing each mapped tag at ~10 Hz.

---

## 24. Phase 8 — Return to Dock (2026-09-27)

### Model (no global coordinates, per project constraint)

The rover uses only what it can see now. State machine in `RETURN_TO_DOCK`
mode (owner `docking_service`), engine at 10 Hz:

    SEEK      rotate in place until the DOCK tag appears (25 s timeout → abort)
    APPROACH  drive steered by tag bearing (P-control); slow inside 0.8 m;
              reached when distance ≤ 0.30 m; tag lost > 1.5 s → SEEK
    ALIGN     rotate until |bearing| ≤ 8° (skipped gracefully if bearing
              unavailable — no camera intrinsics yet)
    DOCKED    stop → STANDBY, DOCK event pushed to /ws/alerts

The dock tag = the tag with `type: "DOCK"` in the tag map (default id 1,
editable via the localization API). Gate refusals (obstacle/timeout) stop
motors and track a blocked window; persistent blocks during approach re-seek.
Manual takeover, `set_mode`, or e-stop cancels cleanly.

### New/changed files

| File | Purpose |
|---|---|
| `services/docking_service.py` | The state machine + engine + session API. |
| `routers/docking.py`, `models/docking.py` | REST endpoints. |
| `services/motion_controller.py`, `services/motor_service.py` | Takeover paths now also cancel docking. |
| `core/config.py` | `SENTRA_DOCK_*` env vars (duties, distances, tolerances, timeouts, steer gain). |
| `services/safety_events.py` | `DOCK` event type. |
| `tests/test_phase8_docking.py` | 26 tests. |

### APIs added (Phase 8)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/dock/return` | start tag-guided docking (rejects: e-stop, no DOCK tag, mode unavailable) |
| `POST /api/v1/dock/cancel` | cancel an active run |
| `GET /api/v1/dock/status` | session state, dock tag id, mode |

### WebSocket events added

`/ws/alerts` `safety_event` `event: "DOCK"`, detail `{event: started\|docked\|cancelled\|search_timeout\|approach_timeout, tag_id?, reason?}`.

### Verification performed (Phase 8)

- 26/26 tests (dock-tag resolution + rejection, SEEK→APPROACH, stop distance → ALIGN, rotation until tolerance → docked → STANDBY, tag-lost → re-SEEK, manual takeover cancel, SEEK timeout abort, e-stop cancel, routers, routes)
- Full regression: **295 passed, 0 failed** across all 8 suites
- Live: status shows dock tag 1; return → SEEK in RETURN_TO_DOCK mode; cancel → STANDBY; 3 dock paths registered.

### Known limitations (Phase 8)

1. Approach steering uses bearing only (P-control); without calibrated intrinsics, `distance_m` is FOV-derived — set `SENTRA_TAG_SIZE_M`/`SENTRA_CAM_HFOV_DEG` accurately for reliable stop distance.
2. ALIGN assumes the tag is roughly ahead; final physical docking (charger contact) is out of scope — the rover stops at the marker.
3. If the dock tag is behind the rover at start, SEEK's 360° rotation handles it, but rotating direction is fixed (+1) per session.
4. No obstacle avoidance during SEEK rotation (gate still blocks motion; front obstacle stops rotation until clear).

---

## 25. Phase 9 — Voice Command System (2026-09-27)

### Model

Speech-to-text stays in the Flutter app; the backend receives **structured**
commands (`POST /api/v1/voice/command`) and resolves targets through the tag
map (exact → prefix → space-insensitive substring). Free-text convenience:
send `{"raw": "sentra go to kitchen"}` — wake-word variants stripped anywhere,
keyword patterns matched with soft word boundaries, bare location names parsed.

Commands: `go_to` (target) → Phase 9 navigation engine · `go_to_dock` →
DOCK-type tag · `start_patrol` → route session or legacy wander · `stop` →
halts all autonomy, STANDBY · `call_user` → acknowledged cleanly (real
workflow lands in Phase 13).

### New/changed files

| File | Purpose |
|---|---|
| `services/navigation_service.py` | Generic go-to-location engine (NAVIGATION mode, owner `navigation_service`): SEEK (rotate, 30 s timeout) → APPROACH (bearing-steered, slow near, arrive ≤ 0.45 m, tag-lost → re-seek) → ARRIVED (STANDBY + event). Gate-active; takeover/e-stop cancels. |
| `services/voice_service.py` | Command registry, fuzzy target resolution, raw-text parser, dispatchers. Never raises; returns `{handled, ...}` with suggestions on failure. |
| `routers/voice.py`, `models/voice.py` | `POST /voice/command`, `GET /voice/commands`. |
| `services/motion_controller.py`, `services/motor_service.py` | Takeover paths now also cancel NAVIGATION. |
| `core/config.py` | `SENTRA_NAV_SEARCH_TIMEOUT_S` (30), `SENTRA_NAV_APPROACH_TIMEOUT_S` (90), `SENTRA_NAV_ARRIVE_M` (0.45). |
| `tests/test_phase9_voice.py` | 35 tests. |

### APIs added (Phase 9)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/voice/command` | structured command or `raw` free text → action result with suggestions on failure |
| `GET /api/v1/voice/commands` | command list + examples for app discovery |

### WebSocket events added

`/ws/alerts` `safety_event` `event: "NAVIGATION"`, detail `{event: started\|arrived\|cancelled\|search_timeout\|approach_timeout, target, tag_id, source, reason?}`.

### Verification performed (Phase 9)

- 35/35 tests (resolution fuzziness, raw parsing incl. mid-utterance wake words, dispatch to all 5 commands, navigation engine arrive/re-seek/timeout, takeover, routers, routes)
- Full regression: **330 passed, 0 failed** across all 9 suites
- Live: go_to "KITCHEN" → Kitchen/SEEK; prefix "kit" resolves; stop → STANDBY; start_patrol → legacy wander; call_user acknowledged; unknown target rejected with known-locations list; unknown raw text rejected with command list.

### Known limitations (Phase 9)

1. Resolution is substring-tolerant but not typo-tolerant (no edit distance yet — add if STT noise demands).
2. Ambiguous substrings resolve to the lowest tag_id; the app should offer `GET /localization/tags` for disambiguation UI.
3. `go_to` requires the target tag to be visible at some point during the run — there is no path planning between locations (arrives in later autonomy work).
4. `call_user` is an acknowledged placeholder until Phase 13.

---

## 26. Phase 10 — Person Detection (2026-09-27)

### Pipeline (phone frames → tracked persons)

```
vision hub (10 Hz) → person_detection queue (newest-only)
                          → detector backend (pluggable)
                          → centroid tracker (stable person_id, gate 150 px)
                          → /person/detections · /person/tracked · PERSON events
```

Backends (swap via `SENTRA_PERSON_BACKEND`, API unchanged):
- `simulation` (default) — driven by `POST /person/simulate` / `inject()`
- `hog` — OpenCV HOG pedestrian SVM, CPU-only, downscale-to-640, no new deps
- future: MobileNet-SSD / YOLO / MediaPipe — implement one `detect(frame)` function

Events: one PERSON event per N tracked updates (`SENTRA_PERSON_EVENT_EVERY`),
debounced through the standard safety-event bridge → `/ws/alerts`.

### New/changed files

| File | Purpose |
|---|---|
| `services/person_detection.py` | Backends + tracker + worker thread + `inject()` + REST-facing queries. Detector failure degrades to no-persons, never crashes. |
| `routers/person.py`, `models/person.py` | detections / tracked / status / simulate. |
| `services/safety_events.py` | `PERSON` event type. |
| `main.py` | Worker lifecycle + vision-hub subscription. |
| `tests/test_phase10_person.py` | 21 tests. |

### APIs added (Phase 10)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/person/detections?limit=` | recent detection events (newest first) |
| `GET /api/v1/person/tracked` | currently-tracked persons + age |
| `GET /api/v1/person/status` | backend health, frames, counts |
| `POST /api/v1/person/simulate` | inject 1–5 synthetic persons (dev) |

### Verification performed (Phase 10)

- 21/21 tests (stable IDs, motion matching, expiry, history, HOG smoke test, worker-through-hub, routers, routes)
- Full regression: **351 passed, 0 failed** — two consecutive full runs (fixed timing-sensitive sleeps in Phase 8/9 tests by polling for state transitions)
- Live: simulate → 2 tracked persons with stable IDs/bboxes; history; 3 vision subscribers registered; 4 person paths.

### Known limitations (Phase 10)

1. HOG is a weak detector (indoor, varied lighting) — intended as a placeholder for an ML backend.
2. Tracker is centroid-based (no appearance features); crossing people may swap IDs.
3. `person_id` is per-boot only; recognition/name binding arrives in Phase 11.
4. Detection runs at ≤ 10 Hz on hub frames; measure Pi CPU before raising.

---

## 27. Phase 11 — Person Recognition (2026-09-27)

### Pipeline

```
vision hub → recognition worker (newest frame)
   → Haar frontal-face detect → face crops → embedder (pluggable)
   → cosine match vs registry → KNOWN (name, person_key) | UNKNOWN
   → UNKNOWN: snapshot JPEG + PERSON_UNKNOWN event (DANGER, 15 s cooldown) → /ws/alerts
```

Embeddings are stored, not raw images (`data/person_registry.json`, atomic
writes, `SENTRA_PERSON_REGISTRY_PATH` to relocate). The `simple` embedder is
dependency-free (32×32 normalized grayscale, cosine, threshold 0.86) and is a
placeholder for FaceNet/ArcFace — swap via one `embed_face()` function, API
unchanged. Registered persons keep stable `person_key`s across restarts.

### New/changed files

| File | Purpose |
|---|---|
| `services/person_registry.py` | Persistent persons + reference embeddings; duplicate names rejected; add-embedding over time; delete removes snapshot. |
| `services/person_recognition.py` | Haar detect + embedder + matcher + worker + unknown-alert cooldown + one-shot `recognize_frame()`. |
| `routers/person_registry.py`, `models/person_registry.py` | REST below. **Contract note:** `register` takes the name as a query param + JPEG raw body (FastAPI cannot bind a JSON model and raw body together). |
| `services/safety_events.py` | `PERSON_UNKNOWN` type. |
| `main.py` | Registry load + recognition worker lifecycle. |
| `tests/test_phase11_recognition.py` | 26 tests. |

### APIs added (Phase 11)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/persons/register?name=` | JPEG body → all faces embedded; returns person record |
| `GET /api/v1/persons` | list (embeddings stay internal) |
| `DELETE /api/v1/persons/{person_key}` | remove person + snapshot |
| `POST /api/v1/persons/recognize` | JPEG body → per-face known/unknown + match |
| `GET /api/v1/persons/status` | backend/haar/threshold/counters |

### WebSocket events added

`/ws/alerts` → `{"type":"safety_event","event":"PERSON_UNKNOWN","severity":"DANGER","detail":{faces, snapshot}}`.

### Verification performed (Phase 11)

- 26/26 tests (registry CRUD + persistence + duplicate rejection, embedder determinism/similarity properties, threshold matching, unknown-alert cooldown, worker-through-hub, router error paths, routes)
- Full regression: **377 passed, 0 failed** across 11 suites
- Live: no-face registration → clean 200 error (fixed an HTTP 500 found by live testing); missing name → 422; recognize endpoint; REST list shows seeded person after restart; 67 paths total.

### Known limitations (Phase 11)

1. The `simple` embedder is light — treat as scaffolding; deploy FaceNet/ArcFace for production accuracy.
2. Haar detects frontal faces only; profiles/glasses/low light reduce recall.
3. Unknown-alert snapshot rate limited by cooldown; bursts of different strangers within 15 s produce one alert.
4. Registration requires a photo with a detectable face (error is explicit otherwise).

## 28. Phase 12 — Fall Detection (2026-09-27)

### Pipeline

```
Phase 10 tracks (person_detection.get_tracked(), ~10 Hz)
   → fall sampler thread (0.25 s poll; NOT a vision-hub subscriber)
   → per-person temporal evidence:
       aspect   = bbox w/h ≥ 1.15  → 'lying'  (standing human is tall-narrow)
   → state machine per person_id → FALL_CONFIRMED is latched
   → safety_events.report("FALL", evidence, DANGER) → /ws/alerts
   → emergency hooks (Phase 13 wires the auto video call) — fire ONCE
```

The sampler deliberately does not subscribe to the vision hub: it polls the
Phase 10 tracker's output so evidence timing decouples from frame rate.

### Per-person state machine

| State | Meaning | Exits |
|---|---|---|
| `NORMAL` | no lying evidence | lying (w/h ≥ 1.15, conf ≥ 0.55) → `POSSIBLE_FALL` |
| `POSSIBLE_FALL` | lying observed; timing started (`since`) | aspect tall again → `NORMAL` (recovered); sustained ≥ 4.0 s → `FALL_CONFIRMED`; person off-frame > 3 s → `NORMAL` |
| `FALL_CONFIRMED` | latched until reset; hooks + event fired once | only `POST /api/v1/fall/reset` |

**Trigger design note:** the NORMAL→POSSIBLE transition uses lying aspect
alone — stationarity was removed as a trigger because *falling itself is fast
movement*. Stillness only matters for confirmation (sustained lying).
Confirmation is decided by comparing **sample timestamps** (no sleeps), so it
is fully deterministic and testable with synthetic timestamps.

### Env tuning (read at import in `services/fall_detection.py`)

| Env var | Default | Purpose |
|---|---|---|
| `SENTRA_FALL_ENABLED` | `true` | master switch (start() no-ops when off) |
| `SENTRA_FALL_POLL_S` | `0.25` | evidence sampling period (~4 Hz) |
| `SENTRA_FALL_ASPECT` | `1.15` | w/h ratio treated as 'lying' |
| `SENTRA_FALL_MOVEMENT_PX` | `25` | stationarity gate (computed, reported in tuning; not a transition gate — see limitations) |
| `SENTRA_FALL_CONFIRM_S` | `4.0` | sustained lying time to confirm |
| `SENTRA_FALL_MIN_CONF` | `0.55` | minimum tracker confidence sampled |

### New/changed files

| File | Purpose |
|---|---|
| `services/fall_detection.py` | Sampler + per-person state machine + latch + once-only side effects; `register_emergency_hook(fn)`, `simulate_fall()`, `reset()`, `get_status()`. |
| `models/fall.py` | Pydantic models for the three endpoints. |
| `routers/fall.py` | REST below; bodies are optional (`body: X = None`) so empty POSTs work. |
| `services/safety_events.py` | `FALL` type (severity DANGER). |
| `main.py` | Lifespan: `fall_detection.start()` after recognition, before `vision_service.start()`; symmetric `stop()` on shutdown. |
| `tests/test_phase12_fall.py` | 23 tests, deterministic via `sample(timestamp=…)` synthetic timestamps. |

### APIs added (Phase 12)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/fall/status` | overall state + per-person states + last confirmed evidence + tuning |
| `POST /api/v1/fall/reset` | acknowledge/clear one person or all (post-emergency) |
| `POST /api/v1/fall/simulate` | drive person through full fall sequence (dev; `duration_s` 0–30) |

### Emergency hook contract (Phase 13 wiring point)

```python
fall_detection.register_emergency_hook(fn)  # fn(evidence: dict) -> None, sync
```

Called **once** per confirmed fall, on the sampler thread, with evidence
`{person_id, state, aspect, confidence, confirmed_at, sustained_s}`. Hook
exceptions are caught and logged — a failing hook can never block detection.
Phase 13 registers an auto video-call hook that bridges to `call_service`.

### WebSocket events added

None new. `/ws/alerts` carries the existing
`{"type":"safety_event","event":"FALL","severity":"DANGER",...}` bridge.

### Verification performed (Phase 12)

- 23/23 tests: standing→NORMAL, single-lying-frame immunity, recovery, sustained confirm + latch + hooks-fire-once + FALL event, reset, crawl behavior, simulate, router handlers, routes (3 fall paths + earlier phases intact)
- Full regression: **400 passed, 0 failed** across 12 suites
- Live on :8099: status NORMAL → simulate person 99 (5 s) → instant `FALL_CONFIRMED` (aspect 2.57), `last_confirmed` populated, `('FALL','DANGER')` in `/api/v1/safety/events`, reset → NORMAL, 70 openapi paths total. Three bugs found and fixed live: trigger required stillness (real falls missed); side effects only on the sampler thread path (simulate didn't fire hooks); missing `global _confirmed_event` (last_confirmed stayed None).

### Known limitations (Phase 12)

1. **Crawling false positive:** a person in a lying-like aspect continuously ≥ 4 s (crawling) WILL confirm — `MOVEMENT_PX`/stationarity is computed but no longer gates transitions. Needs a posture classifier or height-off-floor evidence to fix.
2. Single 2-D bbox aspect cannot distinguish lying from sitting-on-floor or bending far over at low confidence.
3. `POSSIBLE_FALL` expiry (3 s off-frame) uses wall-clock `time.time()` while transitions use sample timestamps — harmless at default poll but mixing clocks.
4. Confirmed state is in-memory only; a backend restart clears the latch (consistent with the rest of the safety state).

## 29. Phase 13 — Emergency Auto-Call (2026-09-27)

### Pipeline

```
fall confirmed (Phase 12) → emergency hook on_fall_confirmed()
   → session opens (RINGING) → invite broadcast over EXISTING /ws/alerts
   → caregiver app auto-opens call screen, connects EXISTING call sockets
        /ws/webrtc/user (signaling — connecting = answering)
        /ws/call/user   (video frames)
   → ACTIVE  … peers leave → ENDED
   → no answer in RING_TIMEOUT_S → MISSED (lazy deadline check, no timer thread)
```

**No new call protocol.** Roles stay `node` (rover phone A) / `user`
(caregiver phone B); the invite message carries the exact paths to connect.
The user's signaling socket connecting is the answer; presence is noted by
`call_ws.py` via `emergency_call.note_presence(role, present)` on
connect/disconnect. One session at a time; re-confirmations while
RINGING/ACTIVE only refresh the evidence; COOLDOWN_S suppresses duplicates
after a terminal state.

### Session lifecycle

| State | Meaning | Exits |
|---|---|---|
| `RINGING` | invite sent, waiting for caregiver | user socket connects → `ACTIVE`; ack → `ENDED`; ring timeout → `MISSED` |
| `ACTIVE` | caregiver on the call | both peers' sockets gone → `ENDED` (ack is accepted but does not end the call) |
| `MISSED` | nobody answered within the ring window | terminal → cooldown |
| `ENDED` | acknowledged or peers left | terminal → cooldown |

`POST /api/v1/fall/reset` also acknowledges (ends) a still-ringing session —
clearing the fall clears the ring.

### Env tuning (read at import in `services/emergency_call.py`)

| Env var | Default | Purpose |
|---|---|---|
| `SENTRA_EMERGENCY_ENABLED` | `true` | master switch (hook returns immediately when off) |
| `SENTRA_EMERGENCY_RING_S` | `30` | time to answer before MISSED |
| `SENTRA_EMERGENCY_COOLDOWN_S` | `60` | min gap between sessions after a terminal state |

### New/changed files

| File | Purpose |
|---|---|
| `services/emergency_call.py` | Session lifecycle, hook `on_fall_confirmed`, thread-safe invite broadcast (`asyncio.run_coroutine_threadsafe`, same pattern as safety_events), lazy ring expiry. |
| `models/emergency.py` | Pydantic models for the three endpoints. |
| `routers/emergency.py` | REST below; ack body optional. |
| `ws_handlers/call_ws.py` | Two `note_presence()` calls in the webrtc handler (connect/finally) — the only touch point on the call path. |
| `services/fall_detection.py` | `reset()` now also acks a ringing emergency. |
| `main.py` | Lifespan: `emergency_call.attach_loop(...)` + `register_with_fall()` after `fall_detection.start()`; router mounted. |
| `tests/test_phase13_emergency_call.py` | 37 tests. |

### APIs added (Phase 13)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/emergency/status` | current session (None when terminal) + last session + tuning |
| `POST /api/v1/emergency/ack` | caregiver acknowledges; ends a RINGING session; ACTIVE calls run on |
| `GET /api/v1/emergency/history` | past sessions (most recent last) |

### WebSocket events added

`/ws/alerts` → `{"type":"emergency_call","action":"invite|acknowledged|missed|ended","session_id","reason":"FALL","severity":"DANGER","state",...}` — the invite variant also carries `evidence` (Phase 12 dict) and `ws` connection hints (`/ws/webrtc/user`, `/ws/call/user`).

### Verification performed (Phase 13)

- 37/37 tests: fall→RINGING, evidence refresh (no duplicate), answer→ACTIVE, role semantics (node alone never answers), peers-gone→ENDED, cooldown, ack, ring-timeout→MISSED, disabled switch, fall-reset silences ring, router, routes (73 paths total)
- Full regression: **437 passed, 0 failed** across 13 suites
- Live on :8099: simulate → RINGING with evidence person 99 → ack → ENDED+acknowledged → second fall (cooldown 0) → new session → MISSED after ring timeout → history `[(EMG-…439, ENDED, True), (EMG-…619, MISSED, False)]`; **real WebSocket client received the invite on /ws/alerts** with the full payload including connection hints.

### Known limitations (Phase 13)

1. Invite is fire-and-forget: if no app is connected to /ws/alerts, the session simply rings out to MISSED (no push-notification/APNs/FCM integration yet).
2. Only the *most recent* evidence is kept on refresh — multiple simultaneous falls collapse into one session/person view.
3. No re-ring or escalation ladder (e.g., second caregiver, SMS) after MISSED.
4. Answer detection uses webrtc-signaling presence only — a caregiver whose app connects signaling without ever sending video still counts as ACTIVE.

## 30. Phase 14 — Notification Service (2026-09-27)

### Pipeline

```
safety_events.report(...)  (every real event: FALL, PERSON_UNKNOWN, PERSON,
   OBSTACLE, CLIFF, ESTOP, PATROL, DOCK, NAVIGATION, …)
   → add_listener() fan-out (Phase 14 seam)
   → notification_service.notify_safety_event()
        → persistent store ~/sentra_data/notifications.json (atomic writes,
          bounded 500, loaded at startup → feed survives restarts)
        → live push on EXISTING /ws/alerts: {"type":"notification", ...}

Flutter Alerts screen (APIS.md §10 contract preserved exactly):
   GET  /api/v1/alerts?severity=…  → real notifications (mock store deleted)
   POST /api/v1/alerts/{id}/ack    → real ack
```

Severity → presentation (display strings kept stable for Flutter):
DANGER titles are prefixed `"Emergency: …"`, WARNING/INFO pass through;
`timestamp` stays `"<HH:MM:SS> • <Location>"`. Person-friendly description
strings are rendered per event type (e.g. FALL names the person and aspect).

### New/changed files

| File | Purpose |
|---|---|
| `services/notification_service.py` | Mirror + persistent store + push + ack/unread/stats; `add_manual()` for injections. |
| `services/safety_events.py` | `add_listener(fn)` fan-out (listener exceptions contained). |
| `routers/alerts.py` | Rewired onto notification_service; **contract unchanged**, ALT-* mocks removed. |
| `models/notifications.py`, `routers/notifications.py` | Richer feed endpoints below. |
| `main.py` | Lifespan: `load()` + `attach_loop()` + `add_listener()` right after the safety wiring; router mounted. |
| `tests/test_phase14_notifications.py` | 39 tests. |
| `tests/test_phase13_emergency_call.py` | Path-count assertion relaxed to `>= 73` (newest suite owns the exact count). |

### APIs added (Phase 14)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/notifications?severity=&limit=` | full-fidelity feed (newest first) + unread count |
| `POST /api/v1/notifications/ack-all` | acknowledge everything, returns count |
| `GET /api/v1/notifications/stats` | totals/unread/by-severity + persistence path |
| `POST /api/v1/notifications/test` | inject one end-to-end (dev) |

Legacy `GET /api/v1/alerts` + `POST /api/v1/alerts/{id}/ack` keep the exact
Flutter response shape (6-field AlertItem) now backed by real data.

### WebSocket events added

`/ws/alerts` → `{"type":"notification","notification":{id, title, timestamp,
description, severity, acknowledged, event_type, location, created_at,
acknowledged_at}}` — alongside the existing `safety_event` and
`emergency_call` payloads.

### Verification performed (Phase 14)

- 39/39 tests: mirroring via listener (incl. debounce suppression), Flutter contract shape + ack + filter, FALL end-to-end, ack-all/unread/stats, **persistence across module reload**, router handlers, routes (77 paths)
- Full regression: **476 passed, 0 failed** across 14 suites
- Live on :8099: fresh feed `[]` (mocks gone) → simulate fall → DANGER `Emergency: Fall detected` notification through `/api/v1/alerts` → legacy ack → manual test injection → ack-all → **real WS client received the `notification` push** → **real server restart** restored 3 notifications with ack state → 77 paths total.

### Known limitations (Phase 14)

1. PERSON presence events mirror at INFO on every Nth update — feeds can accumulate activity noise; no per-type user preferences/muting yet.
2. Store rewrites the whole JSON file per notification (atomic tmp+rename); fine at current volume, would need batching at high event rates.
3. `location` comes from the safety event (`"unknown"` for most reporters) — per-service location tagging needed for meaningful display.
4. Push is fire-and-forget to currently-connected apps; offline devices recover by pulling `GET /notifications` after reconnect (no per-device delivery tracking; proper push ties into the auth/multi-user phase).

## 31. Phase 15 — Device Roles + JWT Auth Enforcement (2026-09-27)

### Model

```
POST /auth/session (OPEN) → JWT {sub, role, permissions, kind, jti, exp}
                          + refresh_token (stored hashed, single-use)
   jti tracked in persistent device registry → revocable + idle-expiring

Enforcement (staged rollout — Flutter ships token plumbing incrementally):
   middleware on ALL mutating REST (POST/PUT/DELETE) except OPEN_PATHS
   /ws/control gated in-handler (?token= or bearer.* subprotocol)
   read-only REST + telemetry/alerts/call WS stay open this phase

Roles: OWNER (all) · GUARD (+ESTOP, control, ack) · GUEST (read + ack only)
Kinds: 'node' (rover phone, via /pair) · 'user' (caregiver, via /auth/session)
```

Scaling-ready: the registry supports N devices per kind, per-token revocation
(logout), wholesale device revocation (stolen phone), refresh rotation, and an
audit table of token rows — the same contract a SQLite/Redis backend or a
multi-unit cloud relay would expose. `SENTRA_AUTH_ENFORCED=false` disables
enforcement for dev/emulator.

### New/changed files

| File | Purpose |
|---|---|
| `services/device_registry.py` | Persistent devices + jti token table, atomic writes, hashed refresh tokens, sliding idle expiry (24 h), 7-day audit grace. |
| `core/auth.py` | Issue/verify/rotate (python-jose), `enforce_auth` REST dependency, `enforce_ws_control` socket gate (accept→close 4401/4403 so clients see the code), `OPEN_PATHS`. |
| `routers/auth.py` | `/session` (additive `refresh_token` + optional name/kind/platform), `/refresh`, `/logout`. |
| `routers/devices.py`, `models/devices.py` | Device management surface below. |
| `models/requests.py` | Additive auth fields + refresh/logout models. |
| `routers/pair.py` | Pairing now upserts into the registry (kind=node); response unchanged. |
| `ws_handlers/control_ws.py` | Token gate before accept. |
| `main.py` | Registry load in lifespan; enforcement middleware (before CORS so 401s carry CORS headers); devices router. |
| `tests/test_phase15_auth.py` | 54 tests. |

### APIs added (Phase 15)

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/refresh` | rotate refresh → new pair; old access jti revoked (OPEN) |
| `POST /api/v1/auth/logout` | revoke one access token by jti (OPEN) |
| `GET /api/v1/devices` · `/stats` | registry + token stats |
| `POST /api/v1/devices/{id}/revoke` · `/unrevoke` | kill/restore a device's access |
| `DELETE /api/v1/devices/{id}` | forget device |
| `GET /api/v1/devices/{id}/tokens` | token audit rows (refresh hashes never leave the server) |
| `POST /api/v1/devices/cleanup` | drop long-dead token rows |

`POST /api/v1/auth/session` keeps the Phase 1 contract (`token, role,
permissions, expires_in`) with `refresh_token` added.

### Enforcement details

- 401 = missing/invalid/revoked/expired token; 403 = role lacks mutation
  permission (GUEST) — both carry `WWW-Authenticate: Bearer`.
- WS close codes: 4401 unauthenticated, 4403 insufficient role (socket is
  accepted first so browsers/Flutter see the application close code).
- Open REST paths even when enforced: ping, auth/session, auth/refresh,
  auth/logout, pair, docs/openapi.
- GET/HEAD/OPTIONS pass through this phase (read-only rollout stage).

### Verification performed (Phase 15)

- 54/54 tests: issue/verify/tracking, token+device revocation, rotation single-use, REST dependency matrix (401/403/open/read-only/switch), WS gate matrix, router handlers, persistence across reload, **middleware end-to-end section** (added after live testing exposed a handler-vs-middleware gap)
- Full regression: **540 passed, 0 failed** across 15 suites
- Live on :8099: mutation no-token → 401; GET → 200; OWNER mutation → 200; GUEST mutation → 403; **real WS probes**: no-token/guest closed with 4401/4403, OWNER connected and received state pushes; login → refresh → old token 401 → logout → token 401; registry persisted across restarts (devices reloaded from disk); 86 paths total. Two real bugs found live: `/auth/logout` missing from OPEN_PATHS (middleware blocked it; handler tests passed — regression section added), and device-revoke reporting 0 tokens killed (count taken after the internal sweep; reordered).

### Known limitations (Phase 15)

1. Read-only REST and telemetry/alerts/call WS remain open until the Flutter app ships token plumbing (next rollout stage moves them behind enforcement with the same dependency).
2. Login is still role-self-selection (no passwords/pairing codes) — fine inside a trusted LAN; real credential verification is the remaining gap for remote access.
3. Device rows persist but token rows survive restarts only while `devices.json` does; a compromised file grants nothing (refresh hashes only) but access tokens stay valid until idle-expiry.
4. `jwt_secret_key` is the shipped default — must be overridden per unit in production (`.env`).
5. Refresh rotation assigns GUARD (role chosen at login, not stored per device); rotating devices should re-login to regain OWNER.

## 32. Phase 16 — Cloud Relay / Remote Access (2026-09-27)

### Model

```
SENTRA unit (Pi, behind NAT)                relay server (VPS)            caregiver app (anywhere)
        └── dial OUT, persistent tunnel ──────►│                             |
                                               │◄── dial OUT, app_hello ─────┘
   app HTTP request:  app → relay → unit → executed LOCALLY against the unit's
   own ASGI app (middleware included) → response back down the tunnel.

   spontaneous: unit DANGER safety events → relay → all bound apps (push)
```

- **No port forwarding** — both sides dial out; the relay only brokers.
- **Auth stays end-to-end**: proxied requests run through the unit's ASGI app
  *with middleware*, so Phase 15 JWTs are verified in the unit exactly like LAN
  traffic (verified: unauthenticated tunnel POST → 401). The relay never sees
  reusable credentials.
- Envelope protocol (JSON over the tunnel WS): `unit_hello`/`hello_ok` (shared
  secret), `http`/`http_response` (b64 body, header whitelist: authorization,
  content-type, accept; 256 KB response cap), `ping`/`pong` heartbeat,
  `push` (DANGER events only — the alarm, not the chatter).

### New/changed files

| File | Purpose |
|---|---|
| `services/relay_client.py` | Unit-side tunnel: reconnect with capped backoff, hello handshake, ASGI dispatch bridge, DANGER push (`send_push` / `_on_safety_event`), status. |
| `tools/relay_server.py` | Reference broker (dev tool): unit/app registries, envelope forwarding; swap for Redis pub/sub + LB replicas in production — protocol unchanged. |
| `routers/relay.py`, `models/relay.py` | `GET /relay/status`, `POST /relay/probe` (self-test through the dispatch bridge). |
| `main.py` | Lifespan: `configure_from_env()` + `attach_loop()` + DANGER listener + `start()`; symmetric `stop()`; router mounted. |
| `tests/test_phase16_relay.py` | 20 tests incl. **in-process E2E against the real relay server**. |

### APIs added (Phase 16)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/relay/status` | enabled/connected/url/uptime/reconnects/last_error (no secret material) |
| `POST /api/v1/relay/probe` | dispatch one request through the local ASGI bridge (self-test) |

### Verification performed (Phase 16)

- 20/20 tests: dispatch bridge (200/401/404 through it), disabled-state lifecycle, **full in-process E2E** — real relay server thread, unit dials in, app binds, proxied GET/POST, DANGER push delivery — plus router handlers and routes (88 paths)
- Full regression: **550 passed, 0 failed** across 16 suites
- Live: relay on :8766 + backend on :8099 with `SENTRA_RELAY_URL` → unit connected; remote app probe: bind `hello_ok` → **login through the tunnel** (200, OWNER) → authenticated `/fall/simulate` through the tunnel (FALL_CONFIRMED) → **`push` envelope received: FALL / DANGER / person 96**. Negative paths verified too: relay down → client records ConnectionRefused and keeps retrying; unauthenticated tunnel POST → 401.

### Known limitations (Phase 16)

1. REST-only tunnel: WebSocket traffic (telemetry/control/call) is not proxied yet — remote video calls need a WS relay mode (protocol has room for a `ws_open`/`ws_frame` envelope pair).
2. The reference relay trusts the unit secret alone; production needs per-unit credentials, TLS, rate limits, and multi-tenant isolation.
3. 256 KB response cap excludes large payloads (video frames, map images) from the tunnel.
4. Pushes are dropped when the tunnel is down (no store-and-forward); the app recovers by pulling notifications on reconnect.
5. One app connection per probe script in tests; the relay supports N bound apps per unit but fan-out under load is untested.

## 33. Phase 17 — Real System Metrics (2026-09-28)

### Model

```
GET /api/v1/system/status
  └─ services/system_metrics.py (psutil; every read individually guarded)
       cpu:      percent (short blocking sample), count, freq, load_avg
       memory:   total/used MB, percent          (swap too)
       disk:     total/used/free GB, percent      (root partition)
       temp:     vcgencmd (Pi) → psutil thermal zones → cached last good
       process:  backend rss / threads / conns / cpu / uptime
```

- **No fake values**: a metric the platform cannot measure comes back `null`
  (temperature on Windows dev boxes), never a plausible-looking constant —
  the app can distinguish "unknown" from "cold".
- Degraded mode: if psutil is somehow absent the endpoint still returns 200
  with `source: "simulated"` and null readouts (uptime via `time.time()`
  fallback).
- vcgencmd probe is cached (one attempt, then short-circuit) so dev boxes
  never pay a 1 s subprocess timeout per request; last good temp is cached
  too (thermal zones can fail transiently).
- Open path: GET requests are unauthenticated by design (Phase 15 policy),
  matching `/ping` / `/telemetry/*`; diagnostics may need to work pre-login.

### New/changed files

| File | Purpose |
|---|---|
| `services/system_metrics.py` | NEW — psutil snapshot: cpu/mem/swap/disk/temp/process, `read_soc_temperature()` with vcgencmd→psutil fallback + caches. |
| `routers/system.py` | Added `GET /system/status` (lazy service import, response model). |
| `models/responses.py` | Added `CpuStatus` / `MemStatus` / `DiskStatus` / `ProcessStatus` / `SystemStatusResponse`. |
| `requirements.txt` | Pinned `psutil>=6.0.0` (was used-but-unpinned since Phase 5). |
| `tests/test_phase17_system.py` | 25 tests (service, vcgencmd cache, degraded mode, handler, E2E via dispatch bridge, routes). |

### APIs added (Phase 17)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/system/status` | Real host health: cpu/memory/swap/disk/temperature_c/process/source. Fields null when unmeasurable. |

### Verification performed (Phase 17)

- 25/25 tests incl. real-psutil snapshot assertions, faked vcgencmd parse
  (52.3 °C) + failed-probe cache, no-psutil degraded mode, and **E2E through
  the Phase 16 ASGI dispatch bridge** (auth middleware active) — 89 paths.
- Full regression: **575 passed, 0 failed** across 17 suites.
- Live on :8099: `/system/status` returned real dev-host metrics
  (cpu 9.2 % of 20 cores @ 3600 MHz, 65345 MB RAM @ 14.8 %, disk 41.5 %,
  rss 98.8 MB, temperature null on Windows as designed) with no token;
  unauthenticated `POST /system/reboot` correctly → 401 (middleware intact).

### Known limitations (Phase 17)

1. `cpu_percent` uses a short blocking sample (~0.2 s) per request — fine for
   diagnostics polling, not for 10 Hz loops.
2. Load average is null on Windows (no OS concept); on the Pi it is real.
3. No historical buffer: point-in-time only; trend charts are the app's job
   (or a later ring-buffer service).
4. Disk is the root partition only; multi-drive Pi setups would need per-mount
   breakdowns.
5. Battery is still telemetry_service sim data — real ADC/battery-hat reading
   is a Pi-hardware task, tracked separately from this phase.

## 34. Phase 18 — Explicit Simulation Mode (2026-09-28)

### Model

```
SENTRA_SIMULATION=true  (read ONCE at import; latched for the process)
  ├─ core/simulation.py    is_active / activate_once / check_env_override /
  │                        preflight_stop / banner / status / describe
  ├─ hardware init points  motor · cliff · ultrasonic · IMU · encoder
  │                        → refuse GPIO / I2C while the flag is set
  ├─ safety layer          force_stop("simulation_preflight") at startup
  └─ observability         Mode: line in the startup banner, loud !!! banner,
                           GET /api/v1/simulation, /system/status.simulation
```

- **Why**: previously the flag was parsed but never read — simulation was an
  *accident* of missing libraries. On a real Pi 5 with lgpio installed,
  `SENTRA_SIMULATION=true` would still have driven real GPIO. Now every init
  point refuses hardware by config, proven by tests with a **fake lgpio**
  (init refused even when `import lgpio` succeeds — the exact Pi risk).
- `activate_once()` is called before every claim attempt: the mode latches at
  the first init and cannot be un-latched; `check_env_override(device, enabled)`
  composes with per-service `*_ENABLED` env vars (simulation wins).
- **No runtime flipping**: the mode is fixed at process start — toggling
  requires a restart, which is the safe direction (real→sim always possible;
  sim→real requires a deliberate reboot with hardware attached).
- Cliff fix: `_simulated` now honours the flag too, so the Phase 4 health
  aggregator reports `SIMULATED` instead of `FRESH` for refused sensors.
- Autonomous actors need no changes: with motors refused, ultrasonic reads
  return `MAX_DIST` (2.0 m) and cliff reads return False, so patrol/dock/nav
  loops run but never see obstacles or cliffs — and can never move hardware.

### New/changed files

| File | Purpose |
|---|---|
| `core/simulation.py` | NEW — mode latch, per-service gate, preflight stop, banner, status/describe. |
| `services/motor_service.py`, `cliff_service.py`, `ultrasonic_service.py`, `imu_service.py`, `encoder_service.py` | Init points call `activate_once()` and refuse GPIO/I2C under the flag; cliff `_simulated` honours it. |
| `services/system_metrics.py` | Snapshot carries `simulation` block (real + degraded mode). |
| `main.py` | Lifespan: `Mode:` log line, `preflight_stop()`, `banner()`; simulation router mounted. |
| `routers/simulation.py`, `models/responses.py` | `GET /api/v1/simulation`, `POST /api/v1/simulation/status` (auth-gated mirror); `SimulationStatus` model. |
| `core/config.py` | Comment upgraded: flag is now an explicit mode backed by `core/simulation.py`. |
| `tests/test_phase18_simulation.py` | 41 tests incl. fake-lgpio real-vs-sim matrix. |

### APIs added (Phase 18)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/simulation` | `{simulation, env_var, latched, hardware_disabled, preflight_stop}` (open, read-only). |
| `POST /api/v1/simulation/status` | Same payload as POST (auth-gated probe mirror). |
| `GET /api/v1/system/status` (extended) | Now includes the `simulation` block in every snapshot. |

### Verification performed (Phase 18)

- 41/41 tests: latch/override/preflight/banner semantics; **fake-lgpio
  real-vs-sim matrix** — real mode claims pins (motor chip, cliff, ultrasonic,
  encoders), sim mode performs ZERO GPIO claims and refuses the I2C bus with
  lgpio importable; router handlers; E2E through the dispatch bridge (incl.
  unauthenticated POST mirror → 401); 91 openapi paths.
- Full regression: **616 passed, 0 failed** across 18 suites.
- Live on :8099, both modes:
  - `SENTRA_SIMULATION=false`: `Mode: REAL (hardware enabled where available)`,
    `/simulation` → `{simulation:false, latched:true, preflight_stop:null}`,
    **no banner** (0 occurrences).
  - `SENTRA_SIMULATION=true`: banner `!!  SENTRA IS RUNNING IN SIMULATION MODE  !!`,
    preflight log `motors forced to zero (simulation_preflight)`, latched
    warning from init points, `/simulation` → `{simulation:true,
    hardware_disabled:true, preflight_stop:{forced:simulation_preflight}}`.

### Known limitations (Phase 18)

1. Telemetry battery/latency values remain simulated in **both** modes — a real
   battery-hat/ADC is a hardware task.
2. `SENTRA_SIMULATION` is process-wide; per-device simulation granularity
   (e.g. real cliff + fake IMU) is composed via `check_env_override` later.
3. The mode is read at import time: modules must be reloaded (tests) or the
   process restarted (production) to change it — intentional.
4. Simulated ultrasonic flat-lines at `MAX_DIST`; a scripted obstacle simulator
   (like `/fall/simulate` for falls) is future work.

## 35. Phase 19 — systemd Service + Pi 5 Deployment (2026-09-28)

### Layout

```
deploy/
  sentra-backend.service   Pi 5 unit: venv uvicorn :8080, Restart=always,
                           ProtectSystem=strict, DeviceAllow gpiochip/i2c,
                           EnvironmentFile=/opt/sentra/.env
  sentra-relay.service     VPS reference relay: tools/relay_server.py on
                           127.0.0.1:8765 (TLS terminated by nginx/Caddy),
                           isolated user, minimal profile
  env.template             every env var (Phases 0-18) with defaults + notes;
                           PLAIN names → config.py pydantic Settings,
                           SENTRA_* names → os.getenv() in modules
  install.sh               idempotent Pi bootstrap: user w/ gpio+i2c+spi →
                           rsync to /opt/sentra → venv → .env (JWT secret
                           auto-generated, never overwritten) → unit →
                           enable --now → wait for /api/v1/ping
  README.md                runbooks for both targets + production checklist
```

- Backend runs as the dedicated `sentra` user (gpio/i2c/spi groups) from
  `/opt/sentra` with its own venv; `Restart=always` + `RestartSec=5s` so a
  rover backend always comes back; `network-online.target` ordering because
  UDP discovery and the relay dial out at startup.
- Relay server gained `--host` (Phase 16 tool): bind loopback behind a TLS
  proxy instead of exposing 8765 to the world; verified live via subprocess.
- **Env-drift guard**: test_phase19 parses env.template and asserts every
  name exists in code — this caught that pydantic Settings has no env prefix,
  so `SENTRA_JWT_SECRET`/`SENTRA_PORT`-style names would have been silently
  ignored; the template uses the project's real conventions
  (`JWT_SECRET_KEY`, `PORT`, …).

### New/changed files

| File | Purpose |
|---|---|
| `deploy/sentra-backend.service` | Hardened systemd unit for the Pi 5 backend. |
| `deploy/sentra-relay.service` | Systemd unit for the VPS relay (loopback bind). |
| `deploy/env.template` | All documented env vars; JWT default forbidden. |
| `deploy/install.sh` | Idempotent installer (bash -n verified). |
| `deploy/README.md` | Unit + relay runbooks, ops commands, checklist. |
| `tools/relay_server.py` | Added `--host` flag (default unchanged 0.0.0.0). |
| `tests/test_phase19_deploy.py` | 34 tests: unit integrity, drift guard, live relay subprocess, route stability. |

### APIs added (Phase 19)

None — deployment phase; route count stays **91** (asserted).

### Verification performed (Phase 19)

- 34/34 tests: both systemd units' structure (ExecStart/EnvironmentFile/
  Restart/hardening/DeviceAllow), installer `bash -n` + idempotency markers,
  env drift guard both directions (≥ 20 names vs the whole codebase),
  runbook checklist, **live relay subprocess** on `--host 127.0.0.1`
  (handshake answered, bad secret → `hello_error`), and route stability (91).
- Full regression: **650 passed, 0 failed** across 19 suites.
- Live on :8099: `/ping` online, startup log `Mode: REAL (hardware enabled
  where available)` (exactly what the unit's ExecStart produces),
  `/relay/status` → `enabled: false` matching the shipped env default.

### Known limitations (Phase 19)

1. systemd semantics (`systemd-analyze verify`, cgroup behaviour) can only be
   fully validated on Linux; on Windows dev the units are validated
   structurally + the installer syntactically.
2. `ReadWritePaths=/opt/sentra ~/sentra_data` assumes the standard paths;
   custom `SENTRA_*_PATH` locations need matching unit edits.
3. Relay scaling (Redis pub/sub, N replicas) is documented, not implemented —
   the reference broker stays single-instance.
4. No log-rotation/monitoring story yet (journald defaults); a watchdog
   (`WatchdogSec` + sd_notify) is a natural Phase 20 hardening item.

## 36. Phase 20 — Final Hardening + Review (2026-09-28)

### Model

```
startup (lifespan)
  ├─ core/hardening.run_checks()   log SECURITY findings (never abort)
  └─ core/sd_notify.heartbeat.start() + notify("READY=1")
       ├─ Type=notify + WatchdogSec=30s → pings WATCHDOG=1 every 15 s
       └─ off-systemd (dev/tests): everything a silent no-op

GET /api/v1/system/status → security block (checked_at_startup, findings[],
                            count, clean) merged into every snapshot
shutdown → notify("STOPPING=1") + heartbeat.stop()
```

- **Hardening checks** (surfaced, not enforced — bricking an assistance rover
  over a default secret is worse than the risk): JWT secret still the shipped
  default → anyone can mint OWNER tokens; `SENTRA_AUTH_ENFORCED=false`;
  relay enabled with default shared secret; relay on cleartext `ws://`.
- **Watchdog** covers the failure mode restart-on-exit cannot: a wedged
  process (GIL-starved threads, blocked C extension) that never exits.
  Minimal stdlib sd_notify client — no systemd Python package; abstract
  `@`-sockets supported.
- Unit upgraded to `Type=notify` + `WatchdogSec=30s` + `TimeoutStartSec=90s`.

### New/changed files

| File | Purpose |
|---|---|
| `core/hardening.py` | NEW — startup security checks + `summary()` for status. |
| `core/sd_notify.py` | NEW — sd_notify client + `WatchdogHeartbeat` (no-ops off systemd). |
| `main.py` | Lifespan: checks at startup, READY=1/heartbeat, STOPPING=1/stop at shutdown. |
| `services/system_metrics.py`, `models/responses.py` | `security` block in `/system/status` (`SecurityFinding`/`SecurityStatus`). |
| `deploy/sentra-backend.service` | `Type=notify`, `WatchdogSec=30s`, `TimeoutStartSec=90s`. |
| `deploy/README.md` | Checklist item: no `SECURITY:` warnings at startup. |
| `tests/test_phase20_hardening.py` | 31 tests incl. cross-platform socket injection + Linux-only real-socket path. |

### APIs added (Phase 20)

None — `GET /api/v1/system/status` extended with the `security` block;
route count stays **91** (asserted).

### Verification performed (Phase 20)

- 31/31 tests: all finding conditions + clean case; **dev-box realism** —
  the shipped JWT default is active here, so the live snapshot is expected to
  be non-clean (a passing suite that reports clean would mean the check is
  broken); off-systemd no-ops; watchdog pings via injected socket incl.
  send-failure degradation; real AF_UNIX path auto-runs on Linux/Pi;
  deploy-unit assertions; routes stable at 91.
- Full regression: **681 passed, 0 failed** across 20 suites.
- Live on :8099: `/ping` online; `SECURITY: [JWT_DEFAULT_SECRET] …` warning
  in the startup log; `security` block served with `count: 1, clean: false`;
  heartbeat correctly silent off-systemd.

### Known limitations (Phase 20)

1. Findings are warnings, not enforcement — by design (assistance device must
   always boot); deployment checklist covers the human gate.
2. `run_checks()` re-evaluates env per call (summary reflects current env,
   not the startup instant) — cheap and honest, but findings could in theory
   differ from the startup log if env changed.
3. `WatchdogHeartbeat` does not verify the event loop is responsive (only
   that the process can run a thread); a loop-liveness ping is future work.
4. Real-socket sd_notify path is exercised on Linux only (auto-skipped on
   Windows dev).

### Plan closure

All 20 phases delivered: **681 tests green across 20 suites, 91 OpenAPI
paths, 9 WS channels, docs through §36.** The §14 audit table above is fully
green. Remaining work is hardware-attach validation on the Pi 5 (real GPIO,
I2C, battery ADC) and production secret rotation — operational tasks beyond
the backend plan.

## 37. Phase 21 — Final App Compatibility Layer (2026-09-28)

When the **final Flutter app spec** arrived (login/rover-pair auth, robot
objects, one multiplexed `/ws` with `{event, data}` envelope, flat alert
arrays, multipart people registry, call lifecycle), the backend already spoke
a different (older, richer) contract — APIS.md `/api/v1/*`. Rather than
rewrite 20 phases of verified internals, Phase 21 adds an **adapter layer**:
both contracts live side by side against the same services.

### Surface

```
REST (prefix /api)                          internal implementation
  POST /auth/login            (OPEN)        core.auth.issue_session(OWNER)
  POST /auth/rover/pair       (OPEN)        node device + GUARD token
  GET  /auth/me                             token → user object
  POST /auth/logout           (OPEN)        jti revocation
  GET  /robots | /robots/{id}               compat_map.robot_object()
  POST /robots/{id}/mode                    app modes → state machine
  POST /robots/{id}/control/{move,stop,brake,estop}
  CRUD /robots/{id}/locations[/...]         tag_map (+ rename_tag)
  POST /robots/{id}/navigate/go-to          navigation_service / dock
  CRUD /robots/{id}/patrol/routes[...]      patrol_service (name↔id mapping)
  POST /robots/{id}/patrol/{start,pause,stop}   pause ≡ stop (documented)
  POST /robots/{id}/dock[/cancel]           docking_service
  GET  /robots/{id}/camera/status           vision stats
  GET  /alerts (flat) · POST /alerts/{id}/dismiss   notification_service
  GET|POST|PUT|DELETE /people[/...]         person_registry (+ notes/update)
  POST /calls/initiate | /{id}/{accept,reject,end}  + WS call_incoming/ended

WS /ws?token=<jwt>   {"event", "data"} both ways
  push: telemetry 1 Hz · sensor_update 2 Hz · apriltag_detected /
        person_detected on set-change · navigation_status 1 Hz during
        autonomy · alert/emergency instant (safety listener) ·
        call_incoming / call_ended
  recv: control_move / control_stop / control_estop → motion controller
        voice_command → voice_service · camera_status relay · ping → pong
```

### Design decisions

- **Bare payloads, not `{"data": …}`**: the spec's own examples (login,
  robots, alerts, people) show plain objects/arrays — matched the examples.
- **Auth bridge**: `admin`→OWNER, `user`→GUARD, rover→node/GUARD; token in
  `?token=` on `/ws` (4401 when invalid under enforcement).
- **Pause ≡ stop**: the patrol engine has no mid-waypoint freeze (safety
  design); the alias stops cleanly and says so.
- **Fixed bug found here** (Phase 15 latent): `device_registry.upsert_device`
  defaulted `kind="user"`, so `issue_session`'s internal bare call clobbered
  `kind="node"` on every rover token re-issue. Default is now `None` = no
  change.
- Alert `type` mapping: FALL→fall, PERSON_*→unknown_person,
  OBSTACLE/CLIFF→obstacle, ESTOP→estop, others→keyword heuristic →
  connection.

### Files

| File | Purpose |
|---|---|
| `services/compat_map.py` | NEW — mappers: robot/user/location/route/person/alert/telemetry/sensor/apriltag/person/nav events. |
| `routers/compat.py` | NEW — all `/api/*` alias endpoints incl. multipart people + call registry. |
| `ws_handlers/compat_ws.py` | NEW — multiplexed `/ws`, push loops, event dispatch, safety→alert/emergency. |
| `services/person_registry.py` | `notes` on records; `update_person()` (name/notes/snapshot). |
| `services/tag_map.py` | `rename_tag()` (partial update). |
| `services/device_registry.py` | `upsert_device(kind=None)` no-change default (bug fix). |
| `core/auth.py` | OPEN_PATHS += `/api/auth/{login,rover/pair,logout}`. |
| `tools/probe_compat_app.py` | NEW — live probe of the whole app story. |
| `tests/test_phase21_compat.py` | NEW — 62 tests. |

### Verification performed (Phase 21)

- 62/62 tests: auth (login/user/rover-pair/me/401s/revoking logout), robots +
  mode transitions + 401/400/404s, control REST incl. real motion-controller
  targets, locations CRUD, go-to, patrol routes/start/pause/stop/delete, dock,
  alerts flat array + dismiss + status flip, people multipart lifecycle,
  calls lifecycle incl. WS push, **live uvicorn WS probe** (pong, telemetry
  envelope, control_move driving the ramp loop), safety→alert+emergency push,
  121 openapi paths.
- Full regression: **745 passed, 0 failed** across 21 suites.
- Live on :8099 (`tools/probe_compat_app.py`): login 200 → rover pair 200
  (role rover) → robots/me 200 → mode manual → estop latched → 7 alerts with
  app fields → calls ringing→ended → WS pong + telemetry (`is_emergency:
  true` honestly reflecting the latched estop) + sensor_update.

### Known limitations (Phase 21)

1. `image_url`/`confidence` on app alerts are null (internal notifications
   carry neither) — app renders the fallback path.
2. `person_detected` events report `is_known: false` — body tracks are
   un-identified by design (recognition is face-embedding-based, one-shot).
3. Call accept/reject/end is backend bookkeeping; media setup still follows
   the Phase 1 webrtc signaling sockets (the rover phone uses these aliases
   for lifecycle UX only).
4. Login accepts any username/password (appliance model, Phase 15 device
   registry still tracks/revokes every issued token).
