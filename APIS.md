# SENTRA — Raspberry Pi 5 Backend API Specification

This document provides the complete API specification required to develop the **Raspberry Pi 5** backend service for the **SENTRA Companion Robotics & Defense Mobile Application**. 

Once implemented on the Raspberry Pi 5, these endpoints will replace the mobile app's mock data with real-time hardware telemetry, RTSP/MJPEG video streams, motor controls, LiDAR spatial data, and incident safety notifications.

---

## 1. System Architecture Overview

```
 ┌───────────────────────────────────────┐
 │       SENTRA Mobile Flutter App       │
 └───────────────────┬───────────────────┘
                     │
    ┌────────────────┼────────────────┐
    │ REST (HTTP)    │ WebSockets     │ Video (MJPEG/RTSP)
    ▼                ▼                ▼
 ┌───────────────────────────────────────┐
 │   Raspberry Pi 5 Backend Gateway      │
 │        (FastAPI / Python 3.11)        │
 └───────────────────┬───────────────────┘
                     │
     ┌───────────────┼───────────────┐
     ▼               ▼               ▼
 ┌─────────┐   ┌──────────┐   ┌────────────┐
 │ Motors  │   │ Camera   │   │ LiDAR &    │
 │ (PWM/I2C)   │ (Libcamera)   │ Sensors    │
 └─────────┘   └──────────┘   └────────────┘
```

- **Host Environment**: Raspberry Pi 5 (8GB RAM, Raspberry Pi OS Bookworm 64-bit)
- **Recommended Backend Stack**: FastAPI (Python 3.11+), `uvicorn`, `websockets`, `opencv-python` / `picamera2`, `pigpio` / `gpiozero`
- **Default Port**: `8080` (HTTP & WebSockets)
- **Video Streaming Port**: `8080` (MJPEG over HTTP) or `8554` (RTSP via MediaMTX)
- **Discovery Protocol**: mDNS (`sentra-unit-01.local`) & UDP broadcast on port `8888`

---

## 2. Master API Endpoint Summary Table

| Category | Endpoint | Method / Protocol | Screen | Description |
|---|---|---|---|---|
| **System** | `/api/v1/ping` | `GET` (HTTP) | Splash / Connect | Gateway ping & health check |
| **System** | `/api/v1/system/info` | `GET` (HTTP) | Ready / Settings | Raspberry Pi hardware & firmware info |
| **System** | `/api/v1/system/reboot` | `POST` (HTTP) | Settings | Trigger Raspberry Pi OS reboot |
| **Auth** | `/api/v1/auth/session` | `POST` (HTTP) | Select Role | Authorize user role (Owner / Guard / Guest) |
| **Pairing** | `/api/v1/pair` | `POST` (HTTP) | Connect Pi | Pair mobile app instance with Pi unit |
| **Telemetry**| `/api/v1/telemetry/initial-sync` | `GET` (HTTP) | Ready & Connected | Initial specs & hardware battery sync |
| **Telemetry**| `/api/v1/telemetry/live` | `GET` (HTTP) | Dashboard | Live snapshot of battery, latency, uptime |
| **Telemetry**| `/ws/telemetry` | `WebSocket` (WS) | Dashboard | 10Hz streaming telemetry feed |
| **Camera** | `/api/v1/camera/stream.mjpg` | `GET` (HTTP/MJPEG) | Live View / Dashboard | Real-time video stream |
| **Camera** | `/api/v1/camera/ir-filter` | `POST` (HTTP) | Live View | Toggle IR Night Vision hardware relay |
| **Camera** | `/api/v1/camera/snapshot` | `POST` (HTTP) | Live View | Capture high-res snapshot to storage |
| **Control** | `/ws/control` | `WebSocket` (WS) | Manual Control | Low-latency D-pad motor control loop (20Hz) |
| **Control** | `/api/v1/robot/mode` | `POST` (HTTP) | Manual Control | Set mode (`PATROL`, `MANUAL`, `STANDBY`) |
| **Control** | `/api/v1/robot/estop` | `POST` (HTTP) | Manual Control | **Emergency E-STOP disarm trigger** |
| **Control** | `/api/v1/robot/estop/reset` | `POST` (HTTP) | Manual Control | Reset E-STOP motor stall state |
| **Control** | `/api/v1/robot/speed` | `POST` (HTTP) | Manual Control | Set locomotion speed throttle (0.1 - 1.2 m/s)|
| **Alerts** | `/api/v1/alerts` | `GET` (HTTP) | Alerts & Dashboard | Retrieve alert log history |
| **Alerts** | `/api/v1/alerts/{id}/ack` | `POST` (HTTP) | Alerts | Acknowledge safety alert item |
| **Alerts** | `/ws/alerts` | `WebSocket` (WS) | Alerts | Push notification on intrusion detection |
| **Settings**| `/api/v1/settings` | `GET` / `PUT` (HTTP) | Settings | Read and update hardware configuration |

---

## 3. Screen-by-Screen API Requirements

---

### Screen 1: Splash Screen (`lib/screens/splash/splash_screen.dart`)
**Role**: System boot check & version handshake.

#### Endpoint 1.1: Health Check
- **URL**: `GET /api/v1/ping`
- **Description**: Quick response to verify backend availability on app startup.
- **Request Headers**: None
- **Response `200 OK`**:
```json
{
  "status": "online",
  "unit_id": "SNT-9042",
  "timestamp": 1757940957
}
```

---

### Screen 2: Welcome Screen (`lib/screens/onboarding/welcome_screen.dart`)
**Role**: App onboarding overview & system status check.

#### Endpoint 2.1: System Version Check
- **URL**: `GET /api/v1/system/info`
- **Description**: Returns hardware model, firmware build, and active system features.
- **Response `200 OK`**:
```json
{
  "unit_name": "SENTRA-Alpha",
  "hardware": "Raspberry Pi 5 (8GB)",
  "firmware_version": "v2.4.12-release",
  "serial_number": "9042-88B",
  "api_version": "v1.0"
}
```

---

### Screen 3: Select Role Screen (`lib/screens/onboarding/select_role_screen.dart`)
**Role**: Access level authorization (Owner / Guard / Guest).

#### Endpoint 3.1: Authenticate Role Session
- **URL**: `POST /api/v1/auth/session`
- **Description**: Creates an authorized session token bound to the selected role.
- **Request Body**:
```json
{
  "role": "OWNER",
  "device_id": "mobile-device-uuid-12345"
}
```
- **Response `200 OK`**:
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "role": "OWNER",
  "permissions": [
    "TELEMETRY_READ",
    "MANUAL_CONTROL",
    "ESTOP_TRIGGER",
    "SETTINGS_WRITE"
  ],
  "expires_in": 86400
}
```

---

### Screen 4: App Permissions Screen (`lib/screens/onboarding/permissions_screen.dart`)
**Role**: System capabilities & hardware permissions check.

#### Endpoint 4.1: Capabilities Discovery
- **URL**: `GET /api/v1/system/capabilities`
- **Description**: Query connected sensors and hardware features.
- **Response `200 OK`**:
```json
{
  "local_network_mdns": true,
  "camera_installed": true,
  "camera_resolution": "1080p",
  "lidar_installed": true,
  "night_vision_ir": true,
  "acoustic_alarm_speaker": true
}
```

---

### Screen 5: Connect to Pi Screen (`lib/screens/connection/connect_pi_screen.dart`)
**Role**: Network discovery & static IP pairing.

#### Protocol 5.1: UDP Broadcast Discovery
- **Protocol**: `UDP` on port `8888`
- **Payload**: Broadcast string `SENTRA_DISCOVER`
- **Pi Response**: `SENTRA_ACK:192.168.1.104:8080:SNT-9042`

#### Endpoint 5.2: Pair Device
- **URL**: `POST /api/v1/pair`
- **Request Body**:
```json
{
  "ip_address": "192.168.1.104",
  "app_instance_id": "app-flutter-uuid"
}
```
- **Response `200 OK`**:
```json
{
  "paired": true,
  "unit_id": "SNT-9042",
  "unit_name": "SENTRA-Alpha",
  "websocket_telemetry_url": "ws://192.168.1.104:8080/ws/telemetry",
  "websocket_control_url": "ws://192.168.1.104:8080/ws/control",
  "stream_url": "http://192.168.1.104:8080/api/v1/camera/stream.mjpg"
}
```

---

### Screen 6: Ready & Connected Screen (`lib/screens/connection/ready_connected_screen.dart`)
**Role**: Hardware verification & initial specs sync.

#### Endpoint 6.1: Initial Telemetry Sync
- **URL**: `GET /api/v1/telemetry/initial-sync`
- **Response `200 OK`**:
```json
{
  "hardware_model": "Raspberry Pi 5 (8GB)",
  "ip_address": "192.168.1.104",
  "ping_latency_ms": 18,
  "battery_level": 87,
  "battery_charging": true,
  "status_badge": "EXCELLENT"
}
```

---

### Screen 7: SENTRA Dashboard Screen (`lib/screens/dashboard/dashboard_screen.dart`)
**Role**: Primary operational hub with live telemetry grid and safety alert previews.

#### Endpoint 7.1: Live Telemetry Snapshot
- **URL**: `GET /api/v1/telemetry/live`
- **Response `200 OK`**:
```json
{
  "operational_state": "Active Perimeter Patrol",
  "zone": "Living Room & Entrance",
  "status": "ARMED",
  "battery": {
    "level": 87,
    "charging": true,
    "delta": "+2.4% charging"
  },
  "latency_ms": 18,
  "uptime_hours": 14.2,
  "patrol_speed_mps": 0.45
}
```

#### Endpoint 7.2: Real-time Telemetry WebSocket
- **Protocol**: `ws://<pi-ip>:8080/ws/telemetry`
- **Frequency**: 10 Hz
- **Push Payload Format**:
```json
{
  "timestamp": 1757940960.12,
  "battery": 87,
  "charging": true,
  "latency_ms": 18,
  "uptime_sec": 51120,
  "speed_mps": 0.45,
  "pos_x": 12.4,
  "pos_y": -4.8,
  "mode": "PATROL"
}
```

---

### Screen 8: Live View & Camera Stream Screen (`lib/screens/live_view/live_view_screen.dart`)
**Role**: Video streaming feed with HUD overlay & IR Night Vision controls.

#### Endpoint 8.1: Video Feed Stream
- **URL**: `GET /api/v1/camera/stream.mjpg`
- **Format**: `multipart/x-mixed-replace; boundary=frame` (MJPEG over HTTP)
- **RTSP Fallback**: `rtsp://<pi-ip>:8554/live`

#### Endpoint 8.2: Toggle IR Night Vision Hardware Relay
- **URL**: `POST /api/v1/camera/ir-filter`
- **Request Body**:
```json
{
  "enabled": true
}
```
- **Response `200 OK`**:
```json
{
  "ir_filter_active": true,
  "mode": "IR_NIGHT_VISION"
}
```

#### Endpoint 8.3: Capture Snapshot Frame
- **URL**: `POST /api/v1/camera/snapshot`
- **Response `200 OK`**:
```json
{
  "file_path": "/var/sentra/snapshots/snap_1757940980.jpg",
  "download_url": "http://192.168.1.104:8080/snapshots/snap_1757940980.jpg"
}
```

---

### Screen 9: Robot Manual Control Screen (`lib/screens/control/manual_control_screen.dart`)
**Role**: D-pad locomotion control, mode switching, speed throttle, and E-STOP trigger.

#### Protocol 9.1: Low-Latency Locomotion Control Loop
- **Protocol**: `ws://<pi-ip>:8080/ws/control`
- **Frequency**: 20 Hz (Send command on joystick/D-pad touch)
- **Client Push Message**:
```json
{
  "linear_velocity": 0.6,
  "angular_velocity": 0.0,
  "direction": "FORWARD"
}
```

#### Endpoint 9.2: Change Operational Mode
- **URL**: `POST /api/v1/robot/mode`
- **Request Body**:
```json
{
  "mode": "MANUAL"
}
```
- **Response `200 OK`**:
```json
{
  "active_mode": "MANUAL",
  "status": "READY"
}
```

#### Endpoint 9.3: **EMERGENCY E-STOP DISARM (Critical)**
- **URL**: `POST /api/v1/robot/estop`
- **Description**: Triggers hardware motor relay kill-switch immediately. Cuts PWM signals within < 5ms.
- **Request Body**: `{}`
- **Response `200 OK`**:
```json
{
  "estop_active": true,
  "motors_disabled": true,
  "status": "E-STOP ACTIVATED"
}
```

#### Endpoint 9.4: Reset E-STOP Stall State
- **URL**: `POST /api/v1/robot/estop/reset`
- **Response `200 OK`**:
```json
{
  "estop_active": false,
  "motors_disabled": false,
  "status": "READY"
}
```

#### Endpoint 9.5: Set Speed Throttle
- **URL**: `POST /api/v1/robot/speed`
- **Request Body**:
```json
{
  "speed_multiplier": 0.5,
  "target_mps": 0.6
}
```
- **Response `200 OK`**:
```json
{
  "speed_multiplier": 0.5,
  "max_speed_mps": 1.2
}
```

---

### Screen 10: SENTRA Alerts & Safety Feed Screen (`lib/screens/alerts/alerts_screen.dart`)
**Role**: Threat history log and real-time safety alert notifications.

#### Endpoint 10.1: Get Alerts Log
- **URL**: `GET /api/v1/alerts?severity=ALL` (or `DANGER`, `WARNING`)
- **Response `200 OK`**:
```json
{
  "alerts": [
    {
      "id": "ALT-101",
      "title": "Motion Anomaly Detected",
      "timestamp": "16:32:04 • Living Room",
      "description": "Humanoid motion pattern detected near south window. Confidence: 94%.",
      "severity": "DANGER",
      "acknowledged": false
    },
    {
      "id": "ALT-102",
      "title": "Battery Low Threshold",
      "timestamp": "15:10:22 • Docking Bay",
      "description": "Battery dropped below 20%. Robot returning to charger.",
      "severity": "WARNING",
      "acknowledged": true
    }
  ]
}
```

#### Endpoint 10.2: Acknowledge Alert Item
- **URL**: `POST /api/v1/alerts/{id}/ack`
- **Response `200 OK`**:
```json
{
  "alert_id": "ALT-101",
  "acknowledged": true
}
```

---

### Screen 11: Settings & Robot Configuration Screen (`lib/screens/settings/settings_screen.dart`)
**Role**: System maintenance, autonomous schedule, and hardware reboot.

#### Endpoint 11.1: Get Configuration Settings
- **URL**: `GET /api/v1/settings`
- **Response `200 OK`**:
```json
{
  "scheduled_autonomous_patrol": true,
  "patrol_interval_hours": 2,
  "auto_ir_night_vision": true,
  "ir_threshold_lux": 10,
  "high_precision_lidar": true,
  "acoustic_intruder_alarm": true,
  "alarm_volume_db": 95
}
```

#### Endpoint 11.2: Update Configuration Settings
- **URL**: `PUT /api/v1/settings`
- **Request Body**:
```json
{
  "scheduled_autonomous_patrol": true,
  "auto_ir_night_vision": false,
  "high_precision_lidar": true,
  "acoustic_intruder_alarm": true
}
```
- **Response `200 OK`**:
```json
{
  "updated": true,
  "message": "Settings applied successfully"
}
```

#### Endpoint 11.3: Trigger Raspberry Pi OS Reboot
- **URL**: `POST /api/v1/system/reboot`
- **Response `200 OK`**:
```json
{
  "reboot_initiated": true,
  "message": "Raspberry Pi 5 rebooting in 3 seconds..."
}
```

---

## 4. Raspberry Pi 5 Starter Implementation Guide (FastAPI Boilerplate)

To quickly build this backend on your Raspberry Pi 5, install FastAPI and Uvicorn:

```bash
pip install fastapi uvicorn websockets opencv-python gpiozero
```

Save the starter file as `main.py` on your Raspberry Pi 5:

```python
from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
import asyncio
import json

app = FastAPI(title="SENTRA Pi 5 Gateway API")

@app.get("/api/v1/ping")
def ping():
    return {"status": "online", "unit_id": "SNT-9042"}

@app.get("/api/v1/telemetry/live")
def get_live_telemetry():
    return {
        "operational_state": "Active Perimeter Patrol",
        "zone": "Living Room & Entrance",
        "status": "ARMED",
        "battery": {"level": 87, "charging": True, "delta": "+2.4% charging"},
        "latency_ms": 18,
        "uptime_hours": 14.2,
        "patrol_speed_mps": 0.45
    }

@app.post("/api/v1/robot/estop")
def trigger_estop():
    # Insert GPIO motor kill logic here (e.g. gpiozero.LED(PIN).off())
    return {"estop_active": True, "motors_disabled": True, "status": "E-STOP ACTIVATED"}

@app.websocket("/ws/control")
async def websocket_control(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        payload = json.loads(data)
        # Apply PWM motor control output here
        print(f"Locomotion PWM: {payload}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

Run on Raspberry Pi 5:
```bash
python3 main.py
```
