"""
Router: Taught route graph (Phase 5, audit P11)
  GET    /api/v1/nav/graph   — edges + stats
  POST   /api/v1/nav/graph   — manually teach an edge {"from": ..., "to": ...}
  DELETE /api/v1/nav/graph   — remove an edge {"from": ..., "to": ...}

The graph grows automatically from successful navigations (observe_traversal);
these endpoints let the user teach/review edges without driving them.
"""

import logging

from fastapi import APIRouter, HTTPException

from services import route_graph

router = APIRouter(prefix="/api/v1/nav", tags=["RouteGraph"])
logger = logging.getLogger(__name__)


@router.get("/graph")
def get_graph():
    return {
        "edges": route_graph.edges(),
        "stats": route_graph.stats(),
    }


@router.post("/graph")
def add_graph_edge(body: dict):
    a = str((body or {}).get("from") or (body or {}).get("frm") or "").strip()
    b = str((body or {}).get("to") or "").strip()
    result = route_graph.add_edge(a, b, source="manual")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "refused"))
    return result


@router.delete("/graph")
def remove_graph_edge(body: dict):
    a = str((body or {}).get("from") or (body or {}).get("frm") or "").strip()
    b = str((body or {}).get("to") or "").strip()
    if not a or not b:
        raise HTTPException(status_code=400, detail="from and to are required")
    if not route_graph.remove_edge(a, b):
        raise HTTPException(status_code=404, detail="unknown edge")
    return {"deleted": True}
