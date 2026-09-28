from typing import Dict, Set, Optional
from fastapi import WebSocket
import logging

logger = logging.getLogger("sentra.connection_manager")

class ConnectionManager:
    """
    Centralized manager for all WebSocket connections.
    Handles registration, roles (USER, ROVER), and heartbeats.
    """
    def __init__(self):
        # active_connections: { websocket: { "role": "USER"|"ROVER", "user_id": "...", "last_heartbeat": float } }
        self.active_connections: Dict[WebSocket, Dict] = {}

    async def connect(self, websocket: WebSocket, role: str, user_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections[websocket] = {
            "role": role,
            "user_id": user_id,
            "last_heartbeat": 0.0  # Updated on heartbeat messages
        }
        logger.info(f"Client connected: role={role}, id={user_id}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            conn_info = self.active_connections.pop(websocket)
            logger.info(f"Client disconnected: role={conn_info['role']}, id={conn_info['user_id']}")

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast(self, message: dict, role_filter: Optional[str] = None):
        """Sends message to all connected clients, optionally filtered by role."""
        for connection, info in self.active_connections.items():
            if role_filter is None or info["role"] == role_filter:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"Broadcast failed for {info['role']}: {e}")

    def get_connections_by_role(self, role: str) -> Set[WebSocket]:
        return {ws for ws, info in self.active_connections.items() if info["role"] == role}

# Singleton instance for the app
connection_manager = ConnectionManager()
