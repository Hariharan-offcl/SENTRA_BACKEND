"""
SENTRA — Service layer.

Phase 1 skeleton: each future capability gets a module here so imports and
API contracts are stable before the implementations land.

    motor_service      — GPIO motors (live; Phase 1 refactored behind core.safety)
    ultrasonic_service — GPIO ultrasonic (live)
    telemetry_service  — telemetry payloads (live)
    camera_service     — Pi camera MJPEG (live; superseded by phone-camera frames later)
    call_service       — video-call frame relay (live)
    udp_discovery      — LAN discovery (live)

Stubs to be implemented in later phases:
    vision_service     — phone-frame ingress → vision consumers (Phase 5+)
    apriltag_service   — Phase 5
    navigation_service — Phase 7
    patrol_service     — Phase 7
    docking_service    — Phase 8
    voice_service      — Phase 9
    person_service     — Phases 10-11
    fall_detection_service — Phase 12
    notification_service   — Phase 14
"""

# Live services are imported by routers directly; stubs are created per phase.
