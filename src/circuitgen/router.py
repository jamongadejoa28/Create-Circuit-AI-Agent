"""Orthogonal multi-terminal schematic router.

The emitter's legacy router only handles two-node straight/L routes.  This
module provides the missing general primitive: grid A* plus incremental tree
growth for buses, rails, and other branched nets.  It is geometry-only and has
no KiCad serialization assumptions.
"""

from __future__ import annotations

import heapq
from functools import lru_cache
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


@lru_cache(maxsize=32)
def _blocked_cells_cached(obstacles: tuple[Box, ...], grid: float, clearance: float) -> frozenset[tuple[int, int]]:
    return frozenset(_blocked_cells(list(obstacles), grid, clearance))


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


def _move_orient(dx: int, dy: int) -> str:
    return "H" if dx != 0 else "V"


def _astar(
    start: tuple[int, int],
    goals: set[tuple[int, int]],
    blocked: set[tuple[int, int]],
    bounds: tuple[int, int, int, int],
    bend_cost: float,
    occupied_orient: dict[tuple[int, int], set[str]] | None = None,
    occupied_endpoints: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]] | None:
    """Route start to the nearest goal; state includes arrival direction.

    The search runs BACKWARDS — every goal cell seeds the frontier and the
    single `start` is the target — purely so the heuristic is a Manhattan
    distance to one point instead of a min() over the goal set. The goal set
    is the growing route tree, so the forward form recomputed that min on
    every node expansion (measured: 1.35M generator calls, 44% of A* time).
    A path's bend count is orientation-independent, so both directions cost
    the same; the result is re-reversed to keep the start->goal orientation.

    Oriented occupancy (foreign wire cells): same-orientation reuse is blocked
    (parallel overlap / hidden short). Opposite orientation may be entered only
    when continuing straight through — a bend on a foreign wire is a KiCad
    junction. Foreign wire endpoints are fully blocked except as terminals.
    """
    min_x, min_y, max_x, max_y = bounds
    gx, gy = start  # the single target of the backward search
    occ = occupied_orient or {}
    occ_ep = occupied_endpoints or set()
    queue: list[tuple[float, float, int, int, int, int]] = []
    parent: dict[tuple[int, int, int, int], tuple[int, int, int, int] | None] = {}
    best: dict[tuple[int, int, int, int], float] = {}
    for sx, sy in goals:
        seed = (sx, sy, 0, 0)
        parent[seed] = None
        best[seed] = 0.0
        heapq.heappush(queue, (float(abs(sx - gx) + abs(sy - gy)), 0.0, sx, sy, 0, 0))
    while queue:
        _f, cost, x, y, pdx, pdy = heapq.heappop(queue)
        state = (x, y, pdx, pdy)
        if cost != best.get(state):
            continue
        if (x, y) == start:
            path = []
            cur = state
            while cur is not None:
                path.append((cur[0], cur[1]))
                cur = parent[cur]
            return path  # already target->source == start->goal
        # Sitting mid-crossing on a foreign wire: must continue straight.
        must_straight = False
        if (pdx or pdy) and (x, y) not in goals:
            here = occ.get((x, y), set())
            if here and _move_orient(pdx, pdy) not in here:
                must_straight = True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if must_straight and (dx, dy) != (pdx, pdy):
                continue
            nxt = (x + dx, y + dy)
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in blocked and nxt != start:
                continue
            if nxt in occ_ep and nxt not in goals and nxt != start:
                continue
            move_o = _move_orient(dx, dy)
            if move_o in occ.get(nxt, set()) and nxt != start:
                continue
            step = 1.0 + (bend_cost if (pdx or pdy) and (dx, dy) != (pdx, pdy) else 0.0)
            ns = (nxt[0], nxt[1], dx, dy)
            nc = cost + step
            if nc >= best.get(ns, float("inf")):
                continue
            best[ns] = nc
            parent[ns] = state
            heuristic = abs(nxt[0] - gx) + abs(nxt[1] - gy)
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
    oriented_occupancy: dict[Point, set[str]] | None = None,
    occupied_endpoints: set[Point] | None = None,
) -> RouteTree | None:
    """Connect all terminals as an orthogonal tree, or return ``None``.

    Each remaining terminal joins the closest point on the existing routed
    tree. This is a rectilinear Steiner-tree approximation; branch attachment
    points become explicit KiCad junction candidates.

    ``blocked_points`` are exact forbidden coordinates (foreign pins, other
    nets' wire endpoints): in KiCad a wire touching ANY pin coordinate connects
    to it, so these must never appear on a route.

    ``oriented_occupancy`` maps world cells to orientations already claimed by
    foreign nets (``{"H"}``, ``{"V"}``, or both). Same-orientation reuse is
    refused; opposite-orientation crossings are allowed when the path goes
    straight through (KiCad does not join crossed wires without a junction).
    """
    if len(terminals) < 2:
        return RouteTree()
    grid_terms = [_snap(p, grid) for p in terminals]
    # every net on a sheet rasterizes the SAME obstacle list; without the
    # cache this sheet-wide raster was rebuilt once per net
    blocked = set(_blocked_cells_cached(tuple(obstacles), grid, clearance))
    for p in blocked_points or ():
        blocked.add(_snap(p, grid))
    blocked -= set(grid_terms)
    occ_grid: dict[tuple[int, int], set[str]] = {}
    for point, orients in (oriented_occupancy or {}).items():
        key = _snap(point, grid)
        if key in grid_terms:
            continue
        occ_grid.setdefault(key, set()).update(orients)
    ep_grid = {
        _snap(p, grid)
        for p in (occupied_endpoints or set())
        if _snap(p, grid) not in grid_terms
    }
    xs = [p[0] for p in grid_terms]; ys = [p[1] for p in grid_terms]
    bounds = (min(xs) - margin_cells, min(ys) - margin_cells, max(xs) + margin_cells, max(ys) + margin_cells)
    tree_cells = {grid_terms[0]}
    remaining = list(grid_terms[1:])
    paths: list[list[Point]] = []
    while remaining:
        terminal = min(remaining, key=lambda p: min(abs(p[0]-x)+abs(p[1]-y) for x, y in tree_cells))
        path_cells = _astar(
            terminal,
            tree_cells,
            blocked,
            bounds,
            bend_cost,
            occupied_orient=occ_grid,
            occupied_endpoints=ep_grid,
        )
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
