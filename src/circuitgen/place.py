"""Placement engine.

heuristic_place produces the canonical {ref: {unit: Placement}} output:

- heuristic_place: Phase 3 label-density mitigation (plan §7.5) — the
  connection strategy stays stub+label (electrically placement-
  independent), but readability follows the conventions the knowledge
  base itself prescribes (schematic-flow-conventions entry / PEFI §7.2.1):
  inputs left, ICs center, outputs right, positive rails in a top row,
  ground symbols in a bottom row, and every decoupling capacitor directly
  beside the IC unit whose power net it serves.

Spacing is derived from pin extents (symbols have no explicit bbox in our
model; the pin envelope plus a stub/label margin is a reliable proxy).
"""

from __future__ import annotations

from .geometry import (
    GRID,
    Placement,
    pin_absolute_position,
    pin_outward_dir,
    pin_stub_end,
)
from .ir import CircuitIR, SymbolDef
from .pins import PinType
from .netnames import GROUND_NAMES


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


# ---------------------------------------------------------------------------


_LABEL_MARGIN = 10.16  # stub (2.54) + ~6-character local label allowance


def _unit_extent(sym: SymbolDef, unit: int) -> tuple[float, float]:
    """Half-extents (x, y) of a unit's pin envelope, with margin."""
    pins = [p for p in sym.pins if p.unit == unit or p.unit == 0]
    if not pins:
        return (7.62, 7.62)
    ex = max(abs(p.x) for p in pins) + _LABEL_MARGIN
    ey = max(abs(p.y) for p in pins) + 5.08
    return ex, ey


def _classify(ir: CircuitIR, symbols: dict[str, SymbolDef]):
    """Split refs into roles; find decoupling caps and their target ICs."""
    net_of: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for ref, pin_no in net.nodes:
            net_of[(ref, str(pin_no))] = net.name

    def kind_of_net(name: str) -> str:
        for net in ir.nets:
            if net.name != name:
                continue
            for ref, pin_no in net.nodes:
                comp = ir.components.get(ref)
                sym = symbols.get(comp.lib_id) if comp else None
                if sym and sym.is_power:
                    try:
                        if sym.pin(pin_no).etype == PinType.PWRIN:
                            return "gnd" if comp.value in GROUND_NAMES else "power"
                    except KeyError:
                        pass
        return "gnd" if name in GROUND_NAMES else "signal"

    roles: dict[str, str] = {}
    decouple_target: dict[str, tuple[str, int]] = {}  # cap ref -> (ic ref, unit)

    ics = [
        r
        for r, c in ir.components.items()
        if not symbols[c.lib_id].is_power
        and (len(symbols[c.lib_id].pins) > 4 or len(symbols[c.lib_id].placed_units()) > 1)
    ]

    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        if sym.is_power:
            roles[ref] = "gnd_sym" if comp.value in GROUND_NAMES else "rail_sym"
        elif ref in ics:
            roles[ref] = "ic"
        elif sym.reference_prefix in ("SW", "J", "BT"):
            roles[ref] = "input"
        elif sym.reference_prefix in ("D", "LS", "BZ") :
            roles[ref] = "output"
        else:
            roles[ref] = "mid"

    # decoupling: a 2-pin C bridging a power-kind net and a gnd-kind net,
    # assigned to the IC unit holding a PWRIN pin on that power net
    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        if sym.reference_prefix != "C" or len(sym.pins) != 2:
            continue
        nets = [net_of.get((ref, p.number)) for p in sym.pins]
        kinds = {n: kind_of_net(n) for n in nets if n}
        power_nets = [n for n, k in kinds.items() if k == "power"]
        if not power_nets or "gnd" not in kinds.values():
            continue
        for ic in ics:
            ic_sym = symbols[ir.components[ic].lib_id]
            for p in ic_sym.pins:
                if p.etype == PinType.PWRIN and net_of.get((ic, p.number)) == power_nets[0]:
                    roles[ref] = "decouple"
                    decouple_target[ref] = (ic, p.unit if p.unit in ic_sym.placed_units() else ic_sym.placed_units()[0])
                    break
            if ref in decouple_target:
                break

    return roles, decouple_target


# --- signal-flow layered placement (topology-based, replaces shelf order) ---

_DRIVER_ETYPES = {"OUTPUT", "PWROUT", "OPENCOLL", "OPENEMIT", "TRISTATE"}


def _signal_edges(ir: CircuitIR, symbols: dict[str, SymbolDef], refs: set[str]):
    """Component adjacency over signal nets: [(a, b, directed_a_to_b)].

    Direction comes from pin electrical types: a net's driver-side member
    points toward its non-driver members. Rails (power-symbol nets, ground
    names) carry no flow information and are skipped."""
    edges: list[tuple[str, str, bool]] = []
    for net in ir.nets:
        if net.name.upper() in GROUND_NAMES:
            continue
        if any(
            symbols[c.lib_id].is_power
            for r, _p in net.nodes
            if (c := ir.components.get(r)) and c.lib_id in symbols
        ):
            continue
        members = [(r, str(p)) for r, p in net.nodes if r in refs]
        if len(members) < 2:
            continue
        drivers = set()
        for r, p in members:
            try:
                if symbols[ir.components[r].lib_id].pin(p).etype.name in _DRIVER_ETYPES:
                    drivers.add(r)
            except KeyError:
                pass
        seen_pairs: set[tuple[str, str]] = set()
        for i, (a, _pa) in enumerate(members):
            for b, _pb in members[i + 1:]:
                if a == b or (a, b) in seen_pairs or (b, a) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
                if a in drivers and b not in drivers:
                    edges.append((a, b, True))
                elif b in drivers and a not in drivers:
                    edges.append((b, a, True))
                else:
                    edges.append((a, b, False))
    return edges


def _flow_columns(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    roles: dict[str, str],
    refs: list[str],
) -> list[list[str]] | None:
    """Order refs into left-to-right signal-flow columns, or None when the
    group carries no discernible flow (falls back to the shelf)."""
    refset = set(refs)
    edges = _signal_edges(ir, symbols, refset)
    if not any(directed for _a, _b, directed in edges):
        return None

    layer: dict[str, float] = {r: 0.0 for r in refs}
    for r in refs:
        if roles.get(r) == "input":
            layer[r] = 0.0
    # longest-path relaxation along driven edges; capped so feedback
    # cycles terminate instead of pushing layers to infinity
    cap = float(len(refs))
    for _ in range(len(refs)):
        changed = False
        for a, b, directed in edges:
            if directed and layer[b] < layer[a] + 1 and layer[a] + 1 <= cap:
                layer[b] = layer[a] + 1
                changed = True
        if not changed:
            break
    # 2-pin passives with no directed edge of their own sit between their
    # neighbours; passives already pushed by a driver keep that layer
    directed_refs = {r for a, b, d in edges if d for r in (a, b)}
    for _ in range(2):
        for r in refs:
            sym = symbols[ir.components[r].lib_id]
            if len(sym.pins) != 2 or roles.get(r) in ("input", "ic") or r in directed_refs:
                continue
            neigh = [
                layer[b if a == r else a]
                for a, b, _d in edges
                if r in (a, b)
            ]
            if neigh:
                layer[r] = sum(neigh) / len(neigh)
    # outputs drift right of everything they hear from
    for r in refs:
        if roles.get(r) == "output":
            neigh = [layer[b if a == r else a] for a, b, _d in edges if r in (a, b)]
            if neigh:
                layer[r] = max(neigh) + 0.5

    columns: dict[float, list[str]] = {}
    for r in refs:
        columns.setdefault(round(layer[r], 2), []).append(r)
    ordered_cols = [columns[k] for k in sorted(columns)]

    # barycenter sweep: order each column by the mean position of already
    # placed neighbours (crossing reduction, one pass)
    pos: dict[str, int] = {}

    def bary(r: str) -> float:
        vals = [
            pos[b if a == r else a]
            for a, b, _d in edges
            if r in (a, b) and (b if a == r else a) in pos
        ]
        return sum(vals) / len(vals) if vals else 1e9

    for col in ordered_cols:
        col.sort(key=lambda r: (bary(r), r))
        for i, r in enumerate(col):
            pos[r] = i
    return ordered_cols


# --- chain alignment: place series passives so the wire router can fire ---

_CHAIN_GAP = 5.08  # facing-pin gap; must stay under emit.DIRECT_WIRE_MAX
_CHAIN_MIN, _CHAIN_MAX_X, _CHAIN_MAX_Y = 15.24, 390.0, 260.0


def _body_box(sym: SymbolDef, unit: int, place: Placement, pad: float = 1.27):
    """Body-approximate box (pin envelope minus pin stick-out), sheet space."""
    pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
    stick = max((p.length for p in pins), default=2.54)
    ex = max(max((abs(p.x) for p in pins), default=5.08) - stick, 2.54) + pad
    ey = max(max((abs(p.y) for p in pins), default=5.08) - stick, 2.54) + pad
    if place.rotation % 180 == 90:
        ex, ey = ey, ex
    return (place.x - ex, place.y - ey, place.x + ex, place.y + ey)


def _facing_rotation(pin, want_dir: tuple[float, float]) -> int | None:
    """Rotation making the pin's outward direction equal want_dir."""
    for rot in (0, 90, 180, 270):
        d = pin_outward_dir(Placement(0.0, 0.0, rot), pin)
        if abs(d[0] - want_dir[0]) < 0.01 and abs(d[1] - want_dir[1]) < 0.01:
            return rot
    return None


def align_chains(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
    roles: dict[str, str],
) -> dict[str, str]:
    """Re-place 2-pin passives that sit on 2-node signal nets so their pins
    face the neighbour across a small gap: series/filter chains (sense R →
    RC → port, dividers, gate resistors) then come out as REAL wires from
    the clearance router instead of stub+label pairs. This is the placement
    half of the user's "analog circuits must be fully drawn" requirement —
    the router alone cannot wire pins the shelf never aligned.

    Movement is conservative: a part moves at most once, only into space
    where its body/pins collide with nothing, else the chain stops there
    and the stub+label fallback keeps correctness.

    Returns a rigid-cluster map {ref: cluster_id}: an aligned satellite and
    its anchor share an id, and any later pass that nudges one member MUST
    move the whole cluster — a lone nudge breaks the pin-facing geometry
    (measured on golden2: the label-dedup loop moved the STM32 +7.62 mm
    after alignment, leaving the BOOT0 resistor's pins inside its pin
    field: silent stacked-pin merges + pin_to_pin ERC).
    """
    net_nodes = {net.name: [(r, str(p)) for r, p in net.nodes] for net in ir.nets}
    net_of: dict[tuple[str, str], str] = {}
    for name, nodes in net_nodes.items():
        for key in nodes:
            net_of[key] = name

    def movable(ref: str) -> bool:
        comp = ir.components.get(ref)
        if comp is None or ref not in placements:
            return False
        sym = symbols[comp.lib_id]
        return (
            not sym.is_power
            and len(sym.pins) == 2
            and len(placements[ref]) == 1
            and roles.get(ref) not in ("ic", "input")
        )

    # occupied space: body boxes + exact pin points of everything placed
    boxes: dict[str, tuple[float, float, float, float]] = {}
    pin_pts: dict[str, list[tuple[float, float]]] = {}
    for ref, units_map in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        for unit, place in units_map.items():
            box = _body_box(sym, unit, place)
            prev = boxes.get(ref)
            boxes[ref] = (
                min(prev[0], box[0]), min(prev[1], box[1]),
                max(prev[2], box[2]), max(prev[3], box[3]),
            ) if prev else box
            pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
            pin_pts.setdefault(ref, []).extend(
                pin_absolute_position(place, p) for p in pins
            )

    def fits(ref: str, box, pts) -> bool:
        if box[0] < _CHAIN_MIN or box[1] < _CHAIN_MIN:
            return False
        if box[2] > _CHAIN_MAX_X or box[3] > _CHAIN_MAX_Y:
            return False
        for other, ob in boxes.items():
            if other == ref:
                continue
            if box[2] > ob[0] and box[0] < ob[2] and box[3] > ob[1] and box[1] < ob[3]:
                return False
            for px, py in pin_pts.get(other, ()):
                if box[0] - 0.01 <= px <= box[2] + 0.01 and box[1] - 0.01 <= py <= box[3] + 0.01:
                    return False
                for qx, qy in pts:
                    if abs(qx - px) < 1.27 and abs(qy - py) < 1.27:
                        return False
        return True

    moved: set[str] = set()
    cluster: dict[str, str] = {}

    def try_place(ref: str, pin, want_dir, target) -> bool:
        """Put `ref` so `pin` sits at `target` facing -want_dir; retry with a
        growing gap when blocked."""
        sym = symbols[ir.components[ref].lib_id]
        rot = _facing_rotation(pin, (-want_dir[0], -want_dir[1]))
        if rot is None:
            return False
        for extra in range(6):
            tx = target[0] + want_dir[0] * extra * 2 * GRID
            ty = target[1] + want_dir[1] * extra * 2 * GRID
            off = pin_absolute_position(Placement(0.0, 0.0, rot), pin)
            place = Placement(round(tx - off[0], 4), round(ty - off[1], 4), rot)
            box = _body_box(sym, 1, place)
            pts = [pin_absolute_position(place, p) for p in sym.pins]
            if fits(ref, box, pts):
                placements[ref] = {1: place}
                boxes[ref] = box
                pin_pts[ref] = pts
                moved.add(ref)
                return True
        return False

    def walk(from_ref: str, from_pin_no: str) -> None:
        """Extend a chain outward from an already-final pin."""
        ref, pin_no = from_ref, from_pin_no
        cid = cluster.get(from_ref, from_ref)
        for _ in range(8):  # chain length guard
            net = net_of.get((ref, pin_no))
            if net is None:
                return
            nodes = net_nodes[net]
            if len(nodes) != 2:
                return
            nxt = next(((r, p) for r, p in nodes if r != ref), None)
            if nxt is None or nxt[0] in moved or not movable(nxt[0]):
                return
            sym = symbols[ir.components[ref].lib_id]
            src = sym.pin(pin_no)
            units_map = placements[ref]
            unit = src.unit if src.unit in units_map else next(iter(units_map))
            place = units_map[unit]
            pos = pin_absolute_position(place, src)
            out = pin_outward_dir(place, src)
            if abs(out[0]) + abs(out[1]) < 0.5:
                return
            target = (round(pos[0] + out[0] * _CHAIN_GAP, 4), round(pos[1] + out[1] * _CHAIN_GAP, 4))
            nxt_sym = symbols[ir.components[nxt[0]].lib_id]
            nxt_pin = nxt_sym.pin(nxt[1])
            if not try_place(nxt[0], nxt_pin, out, target):
                return
            # anchor and satellite are now one rigid body: whoever nudges
            # one later must carry the other (see docstring)
            cluster[from_ref] = cid
            cluster[nxt[0]] = cid
            # continue from the far pin of the part just placed
            far = next(p for p in nxt_sym.pins if p.number != nxt_pin.number)
            ref, pin_no = nxt[0], far.number
        return

    # anchored chains first (IC / connector pins), deterministic order
    for ref in sorted(ir.components):
        if roles.get(ref) in ("ic", "input") and ref in placements:
            sym = symbols[ir.components[ref].lib_id]
            for pin in sorted(sym.pins, key=lambda p: (p.unit, len(p.number), p.number)):
                walk(ref, pin.number)
    # then free passive-passive chains (dividers etc.), first part stays put
    for ref in sorted(ir.components):
        if movable(ref) and ref not in moved:
            sym = symbols[ir.components[ref].lib_id]
            for pin in sym.pins:
                walk(ref, pin.number)
    return cluster


def heuristic_place(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    origin: tuple[float, float] = (25.4, 25.4),
) -> dict[str, dict[int, Placement]]:
    roles, _ = _classify(ir, symbols)
    placements: dict[str, dict[int, Placement]] = {}

    # Build functional tiles.  Before Component.group existed, board-scale
    # drafts were sorted only by reference and became one enormous vertical
    # strip.  Block ownership is now preserved during merge and each block
    # gets a compact shelf-packed tile.
    grouped: dict[str, list[tuple[str, int]]] = {}
    power_refs: list[str] = []
    for ref, comp in ir.components.items():
        if symbols[comp.lib_id].is_power and not (
            comp.lib_id == "power:PWR_FLAG" and comp.group
        ):
            power_refs.append(ref)
            continue
        group = comp.group or "CIRCUIT"
        for unit in symbols[comp.lib_id].placed_units():
            grouped.setdefault(group, []).append((ref, unit))

    role_order = {"input": 0, "ic": 1, "decouple": 2, "mid": 3, "output": 4}
    for items in grouped.values():
        items.sort(key=lambda ru: (role_order.get(roles.get(ru[0], "mid"), 3), ru[0], ru[1]))

    # A2 has ample horizontal space but limited usable height.  Wider tiles
    # keep repeated motor/encoder sections on fewer rows.  Earlier 145 mm
    # tiles plus 15.24 mm gaps pushed a 69-part board below the A2 border.
    TILE_CONTENT_W = 140.0
    SHEET_RIGHT = 570.0
    H_GAP, V_GAP = 7.62, 7.62

    def local_tile(items: list[tuple[str, int]]):
        local: list[tuple[str, int, float, float]] = []
        x = y = 0.0
        row_h = 0.0
        max_x = 0.0
        for ref, unit in items:
            ex, ey = _unit_extent(symbols[ir.components[ref].lib_id], unit)
            width, height = max(2 * ex, 20.32), max(2 * ey, 15.24)
            if x and x + width > TILE_CONTENT_W:
                x = 0.0
                y += row_h + 5.08
                row_h = 0.0
            local.append((ref, unit, x + width / 2, y + height / 2))
            x += width + 5.08
            row_h = max(row_h, height)
            max_x = max(max_x, x - 7.62)
        return local, max(max_x, 30.48), y + row_h

    def layered_tile(items: list[tuple[str, int]]):
        """Signal-flow columns (inputs left, outputs right); None when the
        group carries no discernible flow — the shelf then takes over."""
        refs = sorted({r for r, _u in items})
        if len(refs) < 3:
            return None
        cols = _flow_columns(ir, symbols, roles, refs)
        if cols is None or len(cols) < 2:
            return None
        units_of: dict[str, list[int]] = {}
        for r, u in items:
            units_of.setdefault(r, []).append(u)
        local: list[tuple[str, int, float, float]] = []
        x = 0.0
        max_h = 0.0
        for col in cols:
            col_items = [(r, u) for r in col for u in sorted(units_of.get(r, []))]
            if not col_items:
                continue
            y = 0.0
            col_w = 0.0
            stacked: list[tuple[str, int, float, float, float]] = []
            for r, u in col_items:
                ex, ey = _unit_extent(symbols[ir.components[r].lib_id], u)
                w, h = max(2 * ex, 20.32), max(2 * ey, 15.24)
                stacked.append((r, u, w, h, y))
                y += h + 5.08
                col_w = max(col_w, w)
            for r, u, _w, h, iy in stacked:
                local.append((r, u, x + col_w / 2, iy + h / 2))
            x += col_w + 7.62
            max_h = max(max_h, y - 5.08)
        width = x - 7.62
        if width > SHEET_RIGHT - 50.8:
            return None  # a flow this wide reads worse than the shelf
        return local, max(width, 30.48), max_h

    def group_key(name: str):
        upper = name.upper()
        rank = 0 if upper.startswith("POWER") else 1 if upper.startswith("MCU") else 2
        return (rank, upper)

    tile_x, tile_y = origin[0], origin[1]
    row_height = 0.0
    max_bottom = tile_y
    for group in sorted(grouped, key=group_key):
        local, width, height = layered_tile(grouped[group]) or local_tile(grouped[group])
        # Reserve a heading band even before textual section headings are
        # emitted; this creates the visual whitespace engineers use between
        # repeated channels.
        tile_w = width + 5.08
        tile_h = height + 7.62
        if tile_x > origin[0] and tile_x + tile_w > SHEET_RIGHT:
            tile_x = origin[0]
            tile_y += row_height + V_GAP
            row_height = 0.0
        for ref, unit, lx, ly in local:
            placements.setdefault(ref, {})[unit] = Placement(
                x=_snap(tile_x + 2.54 + lx),
                y=_snap(tile_y + 5.08 + ly),
                rotation=0,
            )
        tile_x += tile_w + H_GAP
        row_height = max(row_height, tile_h)
        max_bottom = max(max_bottom, tile_y + tile_h)

    # Supply symbols form short horizontal rails around the content instead
    # of another component column.  Place grounds at the bottom with ample
    # title-block clearance.
    rail_x = gnd_x = origin[0]
    top_y = max(20.32, origin[1] - 15.24)
    bottom_y = max_bottom + 7.62
    for ref in sorted(power_refs):
        role = roles.get(ref)
        if role == "gnd_sym":
            placements.setdefault(ref, {})[1] = Placement(_snap(gnd_x), _snap(bottom_y), 0)
            gnd_x += 20.32
        else:
            placements.setdefault(ref, {})[1] = Placement(_snap(rail_x), _snap(top_y), 0)
            rail_x += 20.32

    # Gather series/filter chains into facing rows so the wire router can
    # draw them as real wires (runs before the label-endpoint pass, which
    # remains the final electrical-safety net for anything it nudges).
    clusters = align_chains(ir, symbols, placements, roles)

    # Labels are electrical objects in KiCad: two different labels at the
    # exact same stub endpoint silently merge their nets. Body-overlap QA
    # cannot see this. Nudge one whole symbol by one grid until every label
    # endpoint coordinate belongs to only one net. Chain-aligned parts form
    # rigid bodies with their anchor: nudging only one member would leave
    # satellite pins inside the anchor's pin field (measured on golden2).
    for _ in range(96):
        endpoints: dict[tuple[float, float], list[tuple[str, str]]] = {}
        for net in ir.nets:
            for ref, pin_no in net.nodes:
                if ref not in placements:
                    continue
                sym = symbols[ir.components[ref].lib_id]
                try:
                    pin = sym.pin(str(pin_no))
                except KeyError:
                    continue
                units = placements[ref]
                unit = pin.unit if pin.unit in units else next(iter(units))
                end = pin_stub_end(units[unit], pin, 7.62)[1]
                endpoints.setdefault(end, []).append((net.name, ref))
        collision = next(
            (items for items in endpoints.values() if len({n for n, _ in items}) > 1),
            None,
        )
        if collision is None:
            break
        refs = [r for _, r in collision if not symbols[ir.components[r].lib_id].is_power]
        candidates = sorted(set(refs or [r for _, r in collision]))
        # prefer a part outside any rigid cluster; else move the whole cluster
        target = next((r for r in candidates if r not in clusters), candidates[-1])
        cid = clusters.get(target)
        move_refs = (
            [r for r in placements if clusters.get(r) == cid] if cid is not None else [target]
        )
        for mr in move_refs:
            placements[mr] = {
                unit: Placement(_snap(p.x + 2 * GRID), p.y, p.rotation, p.mirror)
                for unit, p in placements[mr].items()
            }

    return placements
