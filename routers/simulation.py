"""
Router: Simulation mode (Phase 18)

  GET /api/v1/simulation          — current mode state
  POST /api/v1/simulation/status  — same payload as GET (probe-friendly)

Both are open paths (read-only); the mode itself is fixed at process start
via SENTRA_SIMULATION and can never be flipped at runtime.
"""

import logging

from fastapi import APIRouter

from models.responses import SimulationStatus
from core.simulation import status as sim_status, describe

router = APIRouter(prefix="/api/v1/simulation", tags=["Simulation"])
logger = logging.getLogger(__name__)


@router.get("", response_model=SimulationStatus)
def get_simulation():
    """Phase 18: report whether the process runs in explicit simulation mode."""
    data = sim_status()
    logger.debug("simulation status: %s", describe())
    return SimulationStatus(**data)


@router.post("/status", response_model=SimulationStatus)
def post_simulation_status():
    """Probe-friendly mirror of GET /simulation (POST stays auth-gated)."""
    return SimulationStatus(**sim_status())
