"""Orthogonal multi-terminal schematic router.

The emitter's legacy router only handles two-node straight/L routes.  This
module provides the missing general primitive: grid A* plus incremental tree
growth for buses, rails, and other branched nets.  It is geometry-only and has
no KiCad serialization assumptions.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

Point = tuple[float, float]
Box = tuple[float, float, float, float]


@dataclass
class RouteTree:
    segments: list[tuple[Point, Point]] = field(default_factory=list)
    junctions: set[Point] = field(default_factory=set)
    paths: list[list[Point]] = field(default_factory=list)


def _snap(point: Point, grid: float) -> tuple[int, int]:
    return round(point[0] / grid), round(point[1] / grid)


def _world(point: tuple[int, int], grid: float) -> Point:
    return round(point[0] * grid, 4), round(point[1] * grid, 4)


def _blocked_cells(obstacles: list[Box], grid: float, clearance: float) -> set[tuple[int, int]]:
    blocked: set[tuple[int, int]] = set()
    for x1, y1, x2, y2 in obstacles:
        lo_x = int((min(x1, x2) - clearance) // grid)
        hi_x = int((max(x1, x2) + clearance) // grid) + 1
        lo_y = int((min(y1, y2) - clearance) // grid)
        hi_y = int((max(y1, y2) + clearance) // grid) + 1
        for x in range(lo_x, hi_x + 1):
            for y in range(lo_y, hi_y + 1):
                wx, wy = _world((x, y), grid)
                if min(x1, x2) - clearance <= wx <= max(x1, x2) + clearance and min(y1, y2) - clearance <= wy <= max(y1, y2) + clearance:
                    blocked.add((x, y))
    return blocked


def _astar(
    start: tuple[int, int],
    goals: set[tuple[int, int]],
    blocked: set[tuple[int, int]],
    bounds: tuple[int, int, int, int],
    bend_cost: float,
) -> list[tuple[int, int]] | None:
    """Route start to the nearest goal; state includes arrival direction."""
    min_x, min_y, max_x, max_y = bounds
    queue: list[tuple[float, float, int, int, int, int]] = []
    heapq.heappush(queue, (0.0, 0.0, start[0], start[1], 0, 0))
    parent: dict[tuple[int, int, int, int], tuple[int, int, int, int] | None] = {
        (start[0], start[1], 0, 0): None
    }
    best: dict[tuple[int, int, int, int], float] = {(start[0], start[1], 0, 0): 0.0}
    while queue:
        _f, cost, x, y, pdx, pdy = heapq.heappop(queue)
        state = (x, y, pdx, pdy)
        if cost != best.get(state):
            continue
        if (x, y) in goals:
            path = []
            cur = state
            while cur is not None:
                path.append((cur[0], cur[1]))
                cur = parent[cur]
            return list(reversed(path))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (x + dx, y + dy)
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in blocked and nxt not in goals:
                continue
            step = 1.0 + (bend_cost if (pdx or pdy) and (dx, dy) != (pdx, pdy) else 0.0)
            ns = (nxt[0], nxt[1], dx, dy)
            nc = cost + step
            if nc >= best.get(ns, float("inf")):
                continue
            best[ns] = nc
            parent[ns] = state
            heuristic = min(abs(nxt[0] - gx) + abs(nxt[1] - gy) for gx, gy in goals)
            heapq.heappush(queue, (nc + heuristic, nc, nxt[0], nxt[1], dx, dy))
    return None


def _compress(path: list[Point]) -> list[Point]:
    if len(path) < 3:
        return path
    out = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = out[-1], path[i], path[i + 1]
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            continue
        out.append(b)
    out.append(path[-1])
    return out


def route_multi_terminal(
    terminals: list[Point],
    obstacles: list[Box],
    *,
    grid: float = 2.54,
    clearance: float = 1.27,
    margin_cells: int = 12,
    bend_cost: float = 2.0,
    blocked_points: list[Point] | None = None,
) -> RouteTree | None:
    """Connect all terminals as an orthogonal tree, or return ``None``.

    Each remaining terminal joins the closest point on the existing routed
    tree. This is a rectilinear Steiner-tree approximation; branch attachment
    points become explicit KiCad junction candidates.

    ``blocked_points`` are exact forbidden coordinates (foreign pins, other
    nets' wire cells): in KiCad a wire touching ANY pin coordinate connects
    to it, so these must never appear on a route.
    """
    if len(terminals) < 2:
        return RouteTree()
    grid_terms = [_snap(p, grid) for p in terminals]
    blocked = _blocked_cells(obstacles, grid, clearance)
    for p in blocked_points or ():
        blocked.add(_snap(p, grid))
    blocked -= set(grid_terms)
    xs = [p[0] for p in grid_terms]; ys = [p[1] for p in grid_terms]
    bounds = (min(xs) - margin_cells, min(ys) - margin_cells, max(xs) + margin_cells, max(ys) + margin_cells)
    tree_cells = {grid_terms[0]}
    remaining = list(grid_terms[1:])
    paths: list[list[Point]] = []
    while remaining:
        terminal = min(remaining, key=lambda p: min(abs(p[0]-x)+abs(p[1]-y) for x, y in tree_cells))
        path_cells = _astar(terminal, tree_cells, blocked, bounds, bend_cost)
        if path_cells is None:
            return None
        path = _compress([_world(p, grid) for p in path_cells])
        paths.append(path)
        tree_cells.update(path_cells)
        remaining.remove(terminal)
    degree: dict[Point, int] = {}
    segments: list[tuple[Point, Point]] = []
    for path in paths:
        for a, b in zip(path, path[1:]):
            segments.append((a, b))
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
    terminal_set = {_world(p, grid) for p in grid_terms}
    junctions = {p for p, d in degree.items() if d >= 3 and p not in terminal_set}
    return RouteTree(segments=segments, junctions=junctions, paths=paths)
