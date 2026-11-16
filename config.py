"""
SENTRA Backend — Global Configuration
All tunable constants and environment variables live here.
"""

import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Unit identity
    unit_id: str = "SNT-9042"
    unit_name: str = "SENTRA-Alpha"
    hardware: str = "Raspberry Pi 5 (8GB)"
    firmware_version: str = "v2.4.12-release"
    serial_number: str = "9042-88B"
    api_version: str = "v1.0"

    # Network
    host: str = "0.0.0.0"
    port: int = 8080
    udp_discovery_port: int = 8888

    # JWT
    jwt_secret_key: str = "SENTRA_SUPER_SECRET_CHANGE_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_seconds: int = 86400

    # Motor defaults
    max_speed_mps: float = 1.2
    default_speed_multiplier: float = 0.5

    # Camera
    camera_snapshot_dir: str = os.path.expanduser("~/sentra_snapshots")
    mjpeg_quality: int = 80
    mjpeg_fps: int = 30

    # IR sensor
    ir_threshold_lux: int = 10

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
