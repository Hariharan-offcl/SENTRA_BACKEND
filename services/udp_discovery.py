"""
SENTRA — UDP discovery service.

Listens on UDP port 8888 for the string "SENTRA_DISCOVER" broadcast
from the mobile app and replies with the Pi's identity and WebSocket URLs.
"""

import asyncio
import logging
import socket

from config import settings

logger = logging.getLogger(__name__)


class UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    """asyncio UDP server that handles discovery broadcasts."""

    def __init__(self, host_ip: str):
        self.host_ip = host_ip
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        logger.info("UDP discovery server listening on port %d", settings.udp_discovery_port)

    def datagram_received(self, data: bytes, addr: tuple):
        message = data.decode(errors="ignore").strip()
        logger.debug("UDP RX from %s: %s", addr, message)

        if message == "SENTRA_DISCOVER":
            response = (
                f"SENTRA_ACK:{self.host_ip}:{settings.port}:{settings.unit_id}"
            )
            self.transport.sendto(response.encode(), addr)
            logger.info("UDP discovery ACK → %s", addr)

    def error_received(self, exc: Exception):
        logger.error("UDP error: %s", exc)

    def connection_lost(self, exc):
        logger.warning("UDP discovery connection lost: %s", exc)


async def start_udp_discovery(host_ip: str) -> None:
    """
    Start the UDP discovery responder as a background asyncio task.
    Call this from the FastAPI lifespan startup hook.
    """
    loop = asyncio.get_event_loop()
    try:
        await loop.create_datagram_endpoint(
            lambda: UDPDiscoveryProtocol(host_ip),
            local_addr=("0.0.0.0", settings.udp_discovery_port),
            allow_broadcast=True,
        )
    except OSError as exc:
        logger.error("Could not start UDP discovery: %s", exc)
