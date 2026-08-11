"""KiCad 10 .kicad_sch emitter — stub-wire + label connection strategy.

Format ground truth: kicad-source-mirror-10.0.5 demo files and
eeschema/sch_file_versions.h (schematic stamp 20260306 for 10.0.5).
Structure notes:
  - No net objects exist in the file: connectivity is coordinate match
    (wire endpoint on pin position) plus same-text labels.
  - Every used symbol's full library definition is embedded in
    (lib_symbols ...), renamed to its full "Lib:Name" id; inner unit
    blocks keep their short names.
  - Every placed symbol lists all its pins as (pin "N" (uuid ...)) and an
    (instances (project ... (path "/<root-uuid>" ...))) block.
  - File tail: (sheet_instances (path "/" (page "1"))) + (embedded_fonts no).

Connection policy (Phase 1): every pin of every net gets a short stub wire
leaving the pin plus a local label carrying the net name. This is SKiDL's
auto_stub fallback applied uniformly — always electrically correct, no
routing needed. Direct wires are used only for aligned facing pins.

Constraint carried by construction: a net that contains a power symbol pin
must be named exactly like the symbol's Value (the power symbol acts as a
global label with that text; a different local label would fight it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .geometry import GRID, Placement, pin_absolute_position, pin_outward_dir, pin_stub_end
from .ir import CircuitIR, SymbolDef
from .sexpr import esc as _esc
from .uuids import uuid_for

SCH_VERSION = 20260306
GENERATOR = "circuitgen"
GENERATOR_VERSION = "0.1.0"

# 2.54 mm put net labels directly on top of long IC pin names.  A standard
# 7.62 mm stub leaves a readable gap while remaining compact.
STUB_LEN = 7.62


def _fmt(v: float) -> str:
    """KiCad-style number: no trailing zeros, ints without decimal point."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


@dataclass
class EmitPlan:
    """Resolved drawing primitives — kept so tests can inspect geometry."""

    wires: list[tuple[tuple[float, float], tuple[float, float], str]] = field(default_factory=list)
    labels: list[tuple[str, float, float, int, str]] = field(default_factory=list)  # text,x,y,rot,justify
    junctions: list[tuple[float, float]] = field(default_factory=list)
    no_connects: list[tuple[float, float]] = field(default_factory=list)
    net_routes: dict[str, str] = field(default_factory=dict)  # net -> direct|l|tree|stubs


def route_metrics(ir: CircuitIR, symbols: dict[str, SymbolDef], plan: EmitPlan) -> dict:
    """Net-level wiring quality — wire-object counts overstate coverage
    (one tree is many segments). Power nets are excluded: they follow the
    power-symbol convention, not solid routing."""
    signal = [
        net.name for net in ir.nets
        if len(net.nodes) >= 2
        and not any(
            symbols[ir.components[r].lib_id].is_power
            for r, _p in net.nodes
            if r in ir.components and ir.components[r].lib_id in symbols
        )
    ]
    wired = [n for n in signal if plan.net_routes.get(n) in ("direct", "l", "tree")]
    return {
        "signal_nets": len(signal),
        "wired_nets": len(wired),
        "stub_nets": len(signal) - len(wired),
        "wired_ratio": round(len(wired) / len(signal), 3) if signal else None,
        "junctions": len(plan.junctions),
        "by_kind": {
            kind: sum(1 for n in signal if plan.net_routes.get(n) == kind)
            for kind in ("direct", "l", "tree", "stubs")
        },
    }


def _label_orientation(dx: float, dy: float) -> tuple[int, str]:
    """Label rotation/justify so text extends away from the stub end."""
    if dx > 0:
        return 0, "left"
    if dx < 0:
        return 180, "right"
    if dy > 0:
        return 270, "right"
    return 90, "left"


PlacementLike = Placement | dict[int, Placement]


def normalize_placements(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, PlacementLike],
) -> dict[str, dict[int, Placement]]:
    """Canonical {ref: {unit: Placement}} form; a bare Placement is accepted
    for single-unit symbols only."""
    out: dict[str, dict[int, Placement]] = {}
    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        units = sym.placed_units()
        p = placements.get(ref)
        if p is None:
            raise KeyError(f"no placement for {ref}")
        if isinstance(p, Placement):
            if len(units) > 1:
                raise ValueError(
                    f"{ref} ({sym.lib_id}) has units {units}; give per-unit placements"
                )
            out[ref] = {units[0]: p}
        else:
            missing = set(units) - set(p)
            if missing:
                raise ValueError(f"{ref}: units {sorted(missing)} not placed")
            out[ref] = dict(p)
    return out


def _instance_unit(pin, units_map: dict[int, Placement], ref: str) -> int:
    """Which placed instance a pin belongs to.

    Unit-0 pins live on the single instance of a single-unit symbol; for
    multi-unit symbols they would appear on EVERY instance, which the
    stub+label strategy can't represent (self-ERC blocks that case first).
    """
    if pin.unit in units_map:
        return pin.unit
    if pin.unit == 0 and len(units_map) == 1:
        return next(iter(units_map))
    raise ValueError(f"{ref}: pin {pin.number} (unit {pin.unit}) has no placed instance")


DIRECT_WIRE_MAX = 60.0  # facing aligned pins closer than this get a real wire
L_WIRE_MAX = 90.0  # Manhattan budget for two-segment routes


def _collect_obstacles(ir, symbols, placements):
    """Foreign-pin points and body boxes a wire must not touch.

    A wire segment passing exactly through ANY pin coordinate connects to
    it in KiCad — the failure that sank the first direct-wire experiment
    (ERC worsened to 36-64 as wires grazed shelf-packed pins).
    """
    from .geometry import pin_absolute_position

    pin_pts: dict[tuple[str, str], tuple[float, float]] = {}
    boxes: dict[str, list[tuple[float, float, float, float]]] = {}
    for ref, units_map in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        for unit, place in units_map.items():
            pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
            for p in pins:
                pin_pts[(ref, p.number)] = pin_absolute_position(place, p)
            stick = max((p.length for p in pins), default=2.54)
            ex = max(max((abs(p.x) for p in pins), default=5.08) - stick, 2.54)
            ey = max(max((abs(p.y) for p in pins), default=5.08) - stick, 2.54)
            if place.rotation % 180 == 90:
                ex, ey = ey, ex
            boxes.setdefault(ref, []).append(
                (place.x - ex, place.y - ey, place.x + ex, place.y + ey)
            )
    return pin_pts, boxes


def _seg_clear(a, b, pin_pts, boxes, skip_refs, skip_pins) -> bool:
    """Axis-aligned segment a→b touches no foreign pin and no foreign body."""
    (x1, y1), (x2, y2) = a, b
    lo_x, hi_x = min(x1, x2), max(x1, x2)
    lo_y, hi_y = min(y1, y2), max(y1, y2)
    for key, (px, py) in pin_pts.items():
        if key in skip_pins:
            continue
        if lo_x - 0.01 <= px <= hi_x + 0.01 and lo_y - 0.01 <= py <= hi_y + 0.01:
            return False
    for ref, ref_boxes in boxes.items():
        if ref in skip_refs:
            continue
        for bx1, by1, bx2, by2 in ref_boxes:
            if hi_x > bx1 + 0.01 and lo_x < bx2 - 0.01 and hi_y > by1 + 0.01 and lo_y < by2 - 0.01:
                return False
    return True


def _try_l_wire(ir, symbols, placements, net, pin_pts, boxes):
    """Two-segment orthogonal route for a 2-node net whose pins are not
    axis-aligned: corner candidates (x2,y1)/(x1,y2), each segment leaving
    pin1 outward and arriving against pin2's outward direction, both
    segments clear of foreign pins/bodies. Analog sheets get fully drawn
    circuits this way (user requirement)."""
    if len(net.nodes) != 2:
        return None
    from .geometry import pin_absolute_position

    info = []
    for ref, pin_no in net.nodes:
        sym = symbols[ir.components[ref].lib_id]
        pin = sym.pin(pin_no)
        units_map = placements[ref]
        place = units_map[_instance_unit(pin, units_map, ref)]
        info.append((ref, str(pin_no), pin_absolute_position(place, pin), pin_outward_dir(place, pin)))
    (r1, p1no, (x1, y1), d1), (r2, p2no, (x2, y2), d2) = info
    if abs(x2 - x1) < 0.01 or abs(y2 - y1) < 0.01:
        return None  # straight case is handled elsewhere
    if abs(x2 - x1) + abs(y2 - y1) > L_WIRE_MAX:
        return None
    skip_refs = {r1, r2}
    skip_pins = {(r1, p1no), (r2, p2no)}
    for corner in ((x2, y1), (x1, y2)):
        cx, cy = corner
        seg1_dir = (cx - x1, cy - y1)
        seg2_dir = (x2 - cx, y2 - cy)
        # leave pin1 along its outward direction, arrive against pin2's
        if d1[0] * seg1_dir[0] + d1[1] * seg1_dir[1] <= 0:
            continue
        if d2[0] * -seg2_dir[0] + d2[1] * -seg2_dir[1] <= 0:
            continue
        if _seg_clear((x1, y1), corner, pin_pts, boxes, skip_refs, skip_pins) and _seg_clear(
            corner, (x2, y2), pin_pts, boxes, skip_refs, skip_pins
        ):
            return (x1, y1), corner, (x2, y2)
    return None


TREE_MAX_NODES = 8


def _stub_corridors(ir, symbols, placements):
    """(ref,pin) -> points a stub wire WOULD occupy if that net falls back.

    Routed nets must avoid every foreign pin's potential stub corridor:
    a route through it would collinearly overlap (= connect to) the stub
    that appears when that other net is not routable."""
    in_net = {(r, str(p)) for n in ir.nets for r, p in n.nodes}
    corridors: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for ref, units_map in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        for unit, place in units_map.items():
            for pin in [p for p in sym.pins if p.unit in (0, unit)] or sym.pins:
                if (ref, pin.number) not in in_net:
                    continue
                start, end = pin_stub_end(place, pin, STUB_LEN)
                dx = (end[0] - start[0]) / max(1, round(abs(end[0] - start[0]) / GRID))
                dy = (end[1] - start[1]) / max(1, round(abs(end[1] - start[1]) / GRID))
                n = round((abs(end[0] - start[0]) + abs(end[1] - start[1])) / GRID)
                corridors[(ref, pin.number)] = [
                    (round(start[0] + dx * i, 4), round(start[1] + dy * i, 4))
                    for i in range(n + 1)
                ]
    return corridors


def _segment_cells(a, b):
    """1.27-grid points covering an axis-aligned segment (over-inclusive:
    off-grid endpoints round outward so blocking never under-covers)."""
    import math

    (x1, y1), (x2, y2) = a, b
    if abs(x1 - x2) < 0.01:
        lo, hi = sorted((y1, y2))
        cells = range(math.floor(lo / GRID + 1e-6), math.ceil(hi / GRID - 1e-6) + 1)
        return [(round(x1, 4), round(i * GRID, 4)) for i in cells]
    lo, hi = sorted((x1, x2))
    cells = range(math.floor(lo / GRID + 1e-6), math.ceil(hi / GRID - 1e-6) + 1)
    return [(round(i * GRID, 4), round(y1, 4)) for i in cells]


def _split_at_endpoints(segments):
    """Split segments at any other segment's endpoint inside them.

    KiCad connects a wire END to a wire INTERIOR only when the interior
    wire is split at that point and a junction dot sits there — the router
    attaches branches mid-segment, so without this pass a branch would
    LOOK connected and be electrically dangling. Returns (segments,
    junction points = degree>=3 endpoints after splitting)."""
    endpoints = {p for seg in segments for p in seg}
    out = []
    for a, b in segments:
        (x1, y1), (x2, y2) = a, b
        vertical = abs(x1 - x2) < 0.01
        inner = [
            (px, py)
            for px, py in endpoints
            if (px, py) not in (a, b)
            and (
                (vertical and abs(px - x1) < 0.01 and min(y1, y2) + 0.01 < py < max(y1, y2) - 0.01)
                or (not vertical and abs(py - y1) < 0.01 and min(x1, x2) + 0.01 < px < max(x1, x2) - 0.01)
            )
        ]
        if not inner:
            out.append((a, b))
            continue
        axis = 1 if vertical else 0
        chain = [a] + sorted(inner, key=lambda p: p[axis], reverse=a[axis] > b[axis]) + [b]
        out.extend((chain[i], chain[i + 1]) for i in range(len(chain) - 1))
    degree: dict[tuple[float, float], int] = {}
    for a, b in out:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    return out, [p for p, d in degree.items() if d >= 3]


def _try_tree_wire(ir, symbols, placements, net, pin_pts, boxes, corridors, routed_cells):
    """Grid-router tree for a 2..TREE_MAX_NODES signal net.

    Terminals enter the grid via a one-cell escape segment along each pin's
    outward direction, so wires always leave pins cleanly. Obstacles: every
    symbol body, every foreign pin POINT (touching one connects in KiCad),
    every foreign potential-stub corridor, and every cell of previously
    routed nets (v1 forbids even legal perpendicular crossings — a refused
    route falls back to stubs, never a wrong wire). Returns (segments,
    junctions, label_at) or None."""
    from .router import route_multi_terminal

    if not (2 <= len(net.nodes) <= TREE_MAX_NODES):
        return None
    if any(symbols[ir.components[r].lib_id].is_power for r, _p in net.nodes):
        return None  # rails keep the power-symbol + stub convention
    own_pins = {(r, str(p)) for r, p in net.nodes}
    terms = []
    for ref, pin_no in net.nodes:
        sym = symbols[ir.components[ref].lib_id]
        pin = sym.pin(pin_no)
        units_map = placements[ref]
        place = units_map[_instance_unit(pin, units_map, ref)]
        pos = pin_absolute_position(place, pin)
        # off-grid pins cannot be met exactly by grid cells; a near-miss
        # wire is a silently unconnected pin — refuse instead
        if any(abs(v / GRID - round(v / GRID)) > 0.005 for v in pos):
            return None
        dx, dy = pin_outward_dir(place, pin)
        if abs(abs(dx) + abs(dy) - 1.0) > 0.01:
            return None
        esc = (round(pos[0] + dx * GRID, 4), round(pos[1] + dy * GRID, 4))
        terms.append((pos, esc, (dx, dy)))
    escapes = [esc for _pos, esc, _d in terms]
    if len(set(escapes)) != len(escapes):
        return None
    blocked = [p for key, p in pin_pts.items() if key not in own_pins]
    blocked += [p for key, pts in corridors.items() if key not in own_pins for p in pts]
    blocked += list(routed_cells)
    blocked_set = {(round(x, 2), round(y, 2)) for x, y in blocked}
    if any((round(x, 2), round(y, 2)) in blocked_set for x, y in escapes):
        return None  # an escape on foreign geometry would silently connect
    tree = route_multi_terminal(
        escapes,
        [b for ref_boxes in boxes.values() for b in ref_boxes],
        grid=GRID,
        clearance=GRID,
        blocked_points=blocked,
    )
    if tree is None:
        return None
    segments = [(pos, esc) for pos, esc, _d in terms] + list(tree.segments)
    segments = [
        (a, b) for a, b in segments if abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.005
    ]
    skip_refs = {r for r, _p in net.nodes}
    for a, b in segments:
        if not _seg_clear(a, b, pin_pts, boxes, skip_refs, own_pins):
            return None  # grid approximation missed something — refuse
    segments, junctions = _split_at_endpoints(segments)
    pos0, _esc0, d0 = terms[0]
    return segments, junctions, (pos0, d0)


def _try_direct_wire(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
    net,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """A 2-node net whose pins face each other on one axis gets a real
    wire pin-to-pin instead of two stubs+labels — visually a schematic,
    not a symbol cloud (user feedback vs the Gemini comparison). The
    netlist round-trip still proves equivalence."""
    if len(net.nodes) != 2:
        return None
    pts, dirs = [], []
    for ref, pin_no in net.nodes:
        sym = symbols[ir.components[ref].lib_id]
        pin = sym.pin(pin_no)
        units_map = placements[ref]
        place = units_map[_instance_unit(pin, units_map, ref)]
        from .geometry import pin_absolute_position

        pts.append(pin_absolute_position(place, pin))
        dirs.append(pin_outward_dir(place, pin))
    (x1, y1), (x2, y2) = pts
    vx, vy = x2 - x1, y2 - y1
    dist = abs(vx) + abs(vy)
    aligned = abs(vx) < 0.01 or abs(vy) < 0.01
    if not aligned or dist < 0.01 or dist > DIRECT_WIRE_MAX:
        return None
    # both pins must point toward each other
    if dirs[0][0] * vx + dirs[0][1] * vy <= 0:
        return None
    if dirs[1][0] * -vx + dirs[1][1] * -vy <= 0:
        return None
    return (x1, y1), (x2, y2)


#: sheet frame margin, and the height the title block reserves bottom-right
_FRAME_MARGIN = 12.7
_TITLE_BLOCK_H = 30.0
_PAPERS = (("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
           ("A2", 594.0, 420.0), ("A1", 841.0, 594.0))


def content_box(
    ir: CircuitIR, symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> tuple[float, float, float, float] | None:
    """(min_x, min_y, max_x, max_y) of everything placed, in sheet mm."""
    xs: list[float] = []
    ys: list[float] = []
    for ref, units in placements.items():
        sym = symbols.get(ir.components[ref].lib_id)
        if sym is None:
            continue
        ex = max((abs(p.x) for p in sym.pins), default=5.08) + 10.16
        ey = max((abs(p.y) for p in sym.pins), default=5.08) + 10.16
        for place in units.values():
            xs += [place.x - ex, place.x + ex]
            ys += [place.y - ey, place.y + ey]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def fit_paper(
    ir: CircuitIR, symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> tuple[str, tuple[float, float]]:
    """Pick the sheet size and the shift that centres the content on it.

    Two things were wrong with choosing the paper from `max(x)` alone. The
    size followed how far RIGHT the content started rather than how big it
    is, so a compact block laid out at x=400 demanded A1. And nothing ever
    moved the content: every sheet was laid out from a fixed top-left origin
    against an A2-width assumption, so a child sheet holding one battery
    printed as a postage stamp in the corner of an A4 page — which is what
    the reader actually sees.

    The shift is grid-snapped so relative geometry is preserved exactly:
    every wire, stub and label keeps its position relative to its pin.
    """
    box = content_box(ir, symbols, placements)
    if box is None:
        return "A4", (0.0, 0.0)
    min_x, min_y, max_x, max_y = box
    width, height = max_x - min_x, max_y - min_y

    paper, page_w, page_h = _PAPERS[-1]
    for cand, w_mm, h_mm in _PAPERS:
        paper, page_w, page_h = cand, w_mm, h_mm
        if (width <= w_mm - 2 * _FRAME_MARGIN
                and height <= h_mm - _FRAME_MARGIN - _TITLE_BLOCK_H):
            break

    left, top = _FRAME_MARGIN, _FRAME_MARGIN
    right, bottom = page_w - _FRAME_MARGIN, page_h - _TITLE_BLOCK_H
    dx = (left + right) / 2 - (min_x + max_x) / 2
    dy = (top + bottom) / 2 - (min_y + max_y) / 2
    # never push content off the top or left edge of an oversized sheet
    dx = max(dx, left - min_x) if width <= right - left else left - min_x
    dy = max(dy, top - min_y) if height <= bottom - top else top - min_y
    snap = lambda v: round(v / GRID) * GRID  # noqa: E731 — keep relative geometry exact
    return paper, (snap(dx), snap(dy))


def build_emit_plan(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> EmitPlan:
    """Stub+label geometry, with real wires (straight or L) whenever the
    route is provably clear of foreign pins and symbol bodies."""
    plan = EmitPlan()
    seen_labels: set[tuple[str, float, float]] = set()
    pin_pts, boxes = _collect_obstacles(ir, symbols, placements)
    corridors = _stub_corridors(ir, symbols, placements)
    routed_cells: set[tuple[float, float]] = set()
    for net in ir.nets:
        direct = _try_direct_wire(ir, symbols, placements, net)
        if direct is not None:
            (x1, y1), (x2, y2) = direct
            r1, p1 = net.nodes[0]
            r2, p2 = net.nodes[1]
            if not _seg_clear(
                (x1, y1), (x2, y2), pin_pts, boxes,
                {r1, r2}, {(r1, str(p1)), (r2, str(p2))},
            ):
                direct = None  # something sits between the facing pins
        if direct is not None:
            (x1, y1), (x2, y2) = direct
            plan.wires.append(((x1, y1), (x2, y2), f"net.{net.name}"))
            plan.net_routes[net.name] = "direct"
            routed_cells.update(_segment_cells((x1, y1), (x2, y2)))
            # name label on the wire itself — without it KiCad auto-names
            # the net (Net-(D1-A)) and the by-name round-trip loses the IR
            # name (caught by the oracle on first run)
            rot, justify = _label_orientation(x2 - x1, y2 - y1)
            plan.labels.append((net.name, x1, y1, rot, justify))
            continue
        l_route = _try_l_wire(ir, symbols, placements, net, pin_pts, boxes)
        if l_route is not None:
            a, corner, b = l_route
            plan.wires.append((a, corner, f"net.{net.name}.a"))
            plan.wires.append((corner, b, f"net.{net.name}.b"))
            plan.net_routes[net.name] = "l"
            rot, justify = _label_orientation(corner[0] - a[0], corner[1] - a[1])
            plan.labels.append((net.name, a[0], a[1], rot, justify))
            for w in plan.wires[-2:]:
                routed_cells.update(_segment_cells(w[0], w[1]))
            continue
        tree = _try_tree_wire(
            ir, symbols, placements, net, pin_pts, boxes, corridors, routed_cells
        )
        if tree is not None:
            segments, junctions, (label_at, label_dir) = tree
            for i, (a, b) in enumerate(segments):
                plan.wires.append((a, b, f"net.{net.name}.t{i}"))
                routed_cells.update(_segment_cells(a, b))
            plan.junctions.extend(junctions)
            plan.net_routes[net.name] = "tree"
            rot, justify = _label_orientation(*label_dir)
            plan.labels.append((net.name, label_at[0], label_at[1], rot, justify))
            continue
        plan.net_routes[net.name] = "stubs"
        for ref, pin_no in net.nodes:
            comp = ir.components[ref]
            sym = symbols[comp.lib_id]
            pin = sym.pin(pin_no)
            units_map = placements[ref]
            place = units_map[_instance_unit(pin, units_map, ref)]
            start, end = pin_stub_end(place, pin, STUB_LEN)
            plan.wires.append((start, end, f"{ref}.{pin_no}"))
            dx, dy = pin_outward_dir(place, pin)
            rot, justify = _label_orientation(dx, dy)
            # Two stubs of one net may end on the same point (facing pins);
            # a second identical label there would duplicate text AND uuid.
            key = (net.name, end[0], end[1])
            if key not in seen_labels:
                seen_labels.add(key)
                plan.labels.append((net.name, end[0], end[1], rot, justify))
    for ref, pin_no in ir.nc_pins:
        comp = ir.components[ref]
        pin = symbols[comp.lib_id].pin(pin_no)
        from .geometry import pin_absolute_position

        units_map = placements[ref]
        place = units_map[_instance_unit(pin, units_map, ref)]
        plan.no_connects.append(pin_absolute_position(place, pin))
    return plan


def _rename_lib_block(raw: str, full_id: str) -> str:
    """Rename a library's (symbol "R" ...) block to (symbol "Device:R" ...)."""
    return re.sub(r'\(symbol\s+"[^"]*"', f'(symbol "{_esc(full_id)}"', raw, count=1)


def _property(
    name: str, value: str, x: float, y: float, *, hide: bool = False, rot: int = 0
) -> str:
    hide_s = "\n\t\t\t(hide yes)" if hide else ""
    return (
        f'\t\t(property "{_esc(name)}" "{_esc(value)}"\n'
        f"\t\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n"
        f"\t\t\t(show_name no)\n"
        f"\t\t\t(do_not_autoplace no)"
        f"{hide_s}\n"
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n"
        f"\t\t)\n"
    )


def emit_schematic(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, PlacementLike],
    plan: EmitPlan | None = None,
    *,
    project_name: str | None = None,
    file_uuid: str | None = None,
    instance_path: str | None = None,
    global_nets: set[str] | None = None,
    hier_nets: set[str] | None = None,
    include_sheet_instances: bool = True,
    extra_body: str = "",
) -> str:
    """Emit one .kicad_sch.

    Hierarchy hooks (all default to the flat single-sheet behavior):
    - project_name/file_uuid/instance_path: child sheets carry the ROOT
      project name and an instances path "/<root-uuid>/<sheet-uuid>".
    - global_nets: labels for these nets emit as global_label (cross-sheet
      connectivity — sheet-pin pairing deliberately avoided).
    - include_sheet_instances: only the root file carries sheet_instances.
    - extra_body: raw s-expression text inserted before the tail (the root
      uses it for its (sheet ...) boxes).
    """
    placements = normalize_placements(ir, symbols, placements)
    paper, offset = fit_paper(ir, symbols, placements)
    if offset != (0.0, 0.0):
        placements = {
            ref: {
                unit: Placement(p.x + offset[0], p.y + offset[1], p.rotation, p.mirror)
                for unit, p in units.items()
            }
            for ref, units in placements.items()
        }
        # wires and labels are derived from the placements, so a plan built
        # against the old coordinates cannot be reused
        plan = None
    if plan is None:
        plan = build_emit_plan(ir, symbols, placements)

    project = project_name or ir.name
    root_uuid = file_uuid or uuid_for(project, ir.name)
    inst_path = instance_path or f"/{root_uuid}"
    global_nets = global_nets or set()
    hier_nets = hier_nets or set()
    out: list[str] = []
    w = out.append


    w("(kicad_sch\n")
    w(f"\t(version {SCH_VERSION})\n")
    w(f'\t(generator "{GENERATOR}")\n')
    w(f'\t(generator_version "{GENERATOR_VERSION}")\n')
    w(f'\t(uuid "{root_uuid}")\n')
    w(f'\t(paper "{paper}")\n')

    # --- lib_symbols: embed each used symbol once, renamed to full lib_id ---
    # Raw blocks start unindented at their "(symbol" token with original
    # (depth-1-relative) inner indentation; shift the whole block one level
    # deeper so it sits at depth 2 under lib_symbols.
    w("\t(lib_symbols\n")
    for lib_id in sorted({c.lib_id for c in ir.components.values()}):
        block = _rename_lib_block(symbols[lib_id].raw_sexp, lib_id).strip("\n")
        first, *rest = block.splitlines()
        w("\t\t" + first.lstrip("\t") + "\n")
        for line in rest:
            w("\t" + line + "\n")
    w("\t)\n")

    for x, y in plan.junctions:
        w(
            f"\t(junction\n\t\t(at {_fmt(x)} {_fmt(y)})\n\t\t(diameter 0)\n"
            f"\t\t(color 0 0 0 0)\n"
            f'\t\t(uuid "{uuid_for(project, ir.name, "junction", _fmt(x), _fmt(y))}")\n\t)\n'
        )

    for x, y in plan.no_connects:
        w(
            f"\t(no_connect\n\t\t(at {_fmt(x)} {_fmt(y)})\n"
            f'\t\t(uuid "{uuid_for(project, ir.name, "nc", _fmt(x), _fmt(y))}")\n\t)\n'
        )

    for (x1, y1), (x2, y2), tag in plan.wires:
        w(
            f"\t(wire\n\t\t(pts\n\t\t\t(xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})\n\t\t)\n"
            f"\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
            f'\t\t(uuid "{uuid_for(project, ir.name, "wire", tag)}")\n\t)\n'
        )

    for text, x, y, rot, justify in plan.labels:
        if text in hier_nets:
            # this net leaves the sheet through a pin on the parent's sheet
            # symbol; the pair is what makes the root a block diagram instead
            # of a row of unconnected rectangles
            w(
                f'\t(hierarchical_label "{_esc(text)}"\n'
                f"\t\t(shape bidirectional)\n"
                f"\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n"
                f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
                f"\t\t\t(justify {justify})\n\t\t)\n"
                f'\t\t(uuid "{uuid_for(project, ir.name, "hlabel", text, _fmt(x), _fmt(y))}")\n\t)\n'
            )
            continue
        if text in global_nets:
            # cross-sheet net: global_label connects project-wide
            w(
                f'\t(global_label "{_esc(text)}"\n'
                f"\t\t(shape bidirectional)\n"
                f"\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n"
                f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
                f"\t\t\t(justify {justify})\n\t\t)\n"
                f'\t\t(uuid "{uuid_for(project, ir.name, "glabel", text, _fmt(x), _fmt(y))}")\n\t)\n'
            )
            continue
        w(
            f'\t(label "{_esc(text)}"\n'
            f"\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n"
            f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
            f"\t\t\t(justify {justify} bottom)\n\t\t)\n"
            f'\t\t(uuid "{uuid_for(project, ir.name, "label", text, _fmt(x), _fmt(y))}")\n\t)\n'
        )

    # --- placed symbols: one instance block per placed unit (demo ground
    # truth: each unit block repeats the same Reference, carries its own
    # (at)/(unit N)/uuid, and lists ALL pins of the whole symbol) ---
    for ref in sorted(ir.components):
        comp = ir.components[ref]
        sym = symbols[comp.lib_id]
        for unit in sorted(placements[ref]):
            place = placements[ref][unit]
            u = uuid_for(project, ir.name, ref, "unit", str(unit))
            w("\t(symbol\n")
            w(f'\t\t(lib_id "{_esc(comp.lib_id)}")\n')
            w(f"\t\t(at {_fmt(place.x)} {_fmt(place.y)} {place.rotation})\n")
            if place.mirror:
                w(f"\t\t(mirror {place.mirror})\n")
            w(f"\t\t(unit {unit})\n")
            w("\t\t(body_style 1)\n")
            w("\t\t(exclude_from_sim no)\n")
            w("\t\t(in_bom yes)\n")
            w("\t\t(on_board yes)\n")
            w("\t\t(in_pos_files yes)\n")
            w("\t\t(dnp no)\n")
            w(f'\t\t(uuid "{u}")\n')
            # Small parts keep compact side annotations; large bodies (ICs)
            # get Reference above and Value below the pin envelope so text
            # never lands on top of in-body pin names.
            unit_pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
            ey = max((abs(p.y) for p in unit_pins), default=2.54)
            if ey > 7.62:
                ref_xy = (place.x, place.y - ey - 3.81)
                val_xy = (place.x, place.y + ey + 3.81)
            else:
                ref_xy = (place.x + 2.54, place.y - 2.54)
                val_xy = (place.x + 2.54, place.y + 2.54)
            w(_property("Reference", ref, *ref_xy, hide=sym.is_power))
            w(_property("Value", comp.value, *val_xy, hide=False))
            w(_property("Footprint", comp.footprint, place.x, place.y, hide=True))
            w(_property("Datasheet", "", place.x, place.y, hide=True))
            w(_property("Description", "", place.x, place.y, hide=True))
            # Some symbols carry duplicate pin numbers (see the library flag
            # duplicate_pin_numbers_are_jumpers); disambiguate their uuids by
            # occurrence index so no two pin entries collide.
            number_counts: dict[str, int] = {}
            for pin in sym.pins:
                idx = number_counts.get(pin.number, 0)
                number_counts[pin.number] = idx + 1
                tag = pin.number if idx == 0 else f"{pin.number}#{idx}"
                w(f'\t\t(pin "{_esc(pin.number)}"\n\t\t\t(uuid "{uuid_for(project, ir.name, ref, "unit", str(unit), "pin", tag)}")\n\t\t)\n')
            w(
                f"\t\t(instances\n"
                f'\t\t\t(project "{_esc(project)}"\n'
                f'\t\t\t\t(path "{inst_path}"\n'
                f'\t\t\t\t\t(reference "{_esc(ref)}")\n'
                f"\t\t\t\t\t(unit {unit})\n"
                f"\t\t\t\t)\n\t\t\t)\n\t\t)\n"
            )
            w("\t)\n")

    if extra_body:
        w(extra_body)
    if include_sheet_instances:
        w('\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n')
    w("\t(embedded_fonts no)\n")
    w(")\n")
    return "".join(out)
