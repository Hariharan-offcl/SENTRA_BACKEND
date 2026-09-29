"""
SENTRA — Taught route graph (Phase 5, audit P11).

An undirected adjacency graph over mapped location names (tag map). It answers
"how do I get from where I am to X?" with a BFS hop list, which the navigation
engine walks as a sequence of per-tag SEEK/APPROACH hops.

How edges are learned:
    * automatically — every successful navigation arrival records the edge
      (previous location → arrived location) via observe_traversal(), so the
      graph grows from real driving ("taught routes");
    * manually — POST /api/v1/nav/graph {"from": ..., "to": ...} for edges
      the user wants registered without driving them.

Storage: ~/sentra_data/route_graph.json  {"edges": [{"from","to","source"}]}

Design constraints honored from the audit:
    * no hardcoded locations — only names that exist in the tag map are
      accepted;
    * unknown/unreachable targets degrade to the existing direct per-tag
      navigation (never refuse a go_to just because the graph is sparse);
    * thread-safe, crash-safe persistence (tmp + os.replace).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from core import config as core_config

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_edges: list[dict] = []          # [{"from": str, "to": str, "source": str}]
_path: str = os.path.expanduser(
    os.getenv("SENTRA_ROUTE_GRAPH_PATH", "~/sentra_data/route_graph.json"))


def load(path: Optional[str] = None) -> None:
    global _path, _edges
    with _lock:
        if path:
            _path = path
        if os.path.exists(_path):
            try:
                with open(_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _edges = [{"from": e["from"], "to": e["to"],
                           "source": e.get("source", "taught")}
                          for e in data.get("edges", [])]
                logger.info("Route graph loaded: %d edges from %s",
                            len(_edges), _path)
                return
            except Exception as exc:
                logger.error("Route graph load failed (%s) — starting empty", exc)
        _edges = []


def _save_locked() -> None:
    tmp = _path + ".tmp"
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"edges": _edges}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _path)


def _has_edge_locked(a: str, b: str) -> bool:
    return any(e for e in _edges
               if {e["from"], e["to"]} == {a, b})


def add_edge(a: str, b: str, source: str = "taught") -> dict:
    """Register an undirected edge between two MAPPED locations."""
    from services import tag_map
    a, b = str(a).strip(), str(b).strip()
    if not a or not b or a == b:
        return {"ok": False, "error": "edge needs two distinct location names"}
    ta, tb = tag_map.find_by_name(a), tag_map.find_by_name(b)
    if ta is None:
        return {"ok": False, "error": f"unknown location: {a!r}"}
    if tb is None:
        return {"ok": False, "error": f"unknown location: {b!r}"}
    a, b = ta["name"], tb["name"]
    with _lock:
        if _has_edge_locked(a, b):
            return {"ok": True, "created": False, "edge": {"from": a, "to": b}}
        _edges.append({"from": a, "to": b, "source": source})
        _save_locked()
    logger.info("Route graph edge added: %s — %s (%s)", a, b, source)
    return {"ok": True, "created": True, "edge": {"from": a, "to": b, "source": source}}


def remove_edge(a: str, b: str) -> bool:
    a, b = str(a).strip(), str(b).strip()
    with _lock:
        before = len(_edges)
        _edges[:] = [e for e in _edges if {e["from"], e["to"]} != {a, b}]
        if len(_edges) != before:
            _save_locked()
            return True
    return False


def observe_traversal(a: str, b: str) -> None:
    """Auto-learn: a successful drive from a to b teaches the edge.
    Never raises; silently ignores unknown/duplicate edges."""
    try:
        if a and b and a != b:
            add_edge(a, b, source="taught")
    except Exception as exc:
        logger.debug("observe_traversal(%r, %r) failed: %s", a, b, exc)


def edges() -> list[dict]:
    with _lock:
        return [dict(e) for e in _edges]


def neighbors(name: str) -> list[str]:
    with _lock:
        return [e["to"] if e["from"] == name else e["from"]
                for e in _edges if name in (e["from"], e["to"])]


def find_path(start: str, goal: str) -> Optional[list[str]]:
    """BFS shortest path of location names, inclusive of both ends.
    None when either node is unknown in the graph or unreachable."""
    if not start or not goal or start == goal:
        return None
    with _lock:
        adj: dict[str, list[str]] = {}
        for e in _edges:
            adj.setdefault(e["from"], []).append(e["to"])
            adj.setdefault(e["to"], []).append(e["from"])
    if start not in adj or goal not in adj:
        return None
    from collections import deque
    q = deque([[start]])
    seen = {start}
    while q:
        path = q.popleft()
        node = path[-1]
        for nb in adj.get(node, []):
            if nb in seen:
                continue
            new = path + [nb]
            if nb == goal:
                return new
            seen.add(nb)
            q.append(new)
    return None


def stats() -> dict:
    with _lock:
        nodes = {e["from"] for e in _edges} | {e["to"] for e in _edges}
        return {
            "edges": len(_edges),
            "nodes": len(nodes),
            "taught": sum(1 for e in _edges if e.get("source") == "taught"),
            "manual": sum(1 for e in _edges if e.get("source") == "manual"),
            "path": _path,
        }
