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

    from .interfaces import analyze_interfaces

    interfaces = analyze_interfaces(ir, symbols)

    def kind_of_net(name: str) -> str:
        interface = interfaces.get(name)
        return interface.kind if interface else ("gnd" if name in GROUND_NAMES else "signal")

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

    # A connector is an output when a non-connector output pin drives one of
    # its signal nets. Reference prefix alone previously put every J on the
    # left, including output headers such as TIMER_OUT.
    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        if sym.reference_prefix != "J":
            continue
        driven_from_board = False
        for net in ir.nets:
            if not any(r == ref for r, _pin in net.nodes):
                continue
            interface = interfaces.get(net.name)
            if interface and interface.kind == "signal" and any(
                driver != ref for driver in interface.drivers
            ):
                driven_from_board = True
                break
        roles[ref] = "output" if driven_from_board else "input"

    # decoupling: a 2-pin C bridging a power-kind net and a gnd-kind net,
    # assigned to the IC unit holding a PWRIN pin on that power net
    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        if sym.reference_prefix != "C" or len(sym.pins) != 2:
            continue
        nets = [net_of.get((ref, p.number)) for p in sym.pins]
        kinds = {n: kind_of_net(n) for n in nets if n}
        power_nets = [n for n, k in kinds.items() if k == "power"]
        if not power_nets or "ground" not in kinds.values():
            continue
        targets: list[tuple[str, int]] = []
        for ic in ics:
            ic_sym = symbols[ir.components[ic].lib_id]
            for p in ic_sym.pins:
                if p.etype == PinType.PWRIN and net_of.get((ic, p.number)) == power_nets[0]:
                    targets.append((
                        ic,
                        p.unit if p.unit in ic_sym.placed_units() else ic_sym.placed_units()[0],
                    ))
                    break
        # A shared rail/GND pair alone does not identify which IC a capacitor
        # serves. Moving it beside an arbitrary small peripheral made every
        # MCU bypass capacitor pile onto the same sensor. Only localize when
        # topology yields one unambiguous target.
        if (len(targets) == 1
                and len(symbols[ir.components[targets[0][0]].lib_id].pins) <= 16):
            roles[ref] = "decouple"
            decouple_target[ref] = targets[0]

    # A two-pin capacitor from one signal net to ground is a local shunt
    # element (timing, control filtering, reset delay, ...). When that signal
    # reaches exactly one small IC, topology identifies a unique physical
    # owner without relying on net names or prompt vocabulary. Treat it like
    # a local bypass for placement; dense MCUs remain excluded above.
    for ref, comp in ir.components.items():
        sym = symbols[comp.lib_id]
        if (ref in decouple_target or sym.reference_prefix != "C"
                or len(sym.pins) != 2):
            continue
        pin_nets = [net_of.get((ref, p.number)) for p in sym.pins]
        if not any(n and kind_of_net(n) == "ground" for n in pin_nets):
            continue
        signal_nets = [n for n in pin_nets if n and kind_of_net(n) == "signal"]
        if len(signal_nets) != 1:
            continue
        signal = signal_nets[0]
        targets = []
        for net in ir.nets:
            if net.name != signal:
                continue
            for owner, pin_no in net.nodes:
                if owner not in ics:
                    continue
                owner_sym = symbols[ir.components[owner].lib_id]
                if len(owner_sym.pins) > 16:
                    continue
                pin = owner_sym.pin(str(pin_no))
                targets.append((
                    owner,
                    pin.unit if pin.unit in owner_sym.placed_units()
                    else owner_sym.placed_units()[0],
                ))
        targets = sorted(set(targets))
        if len(targets) == 1:
            roles[ref] = "decouple"
            decouple_target[ref] = targets[0]

    return roles, decouple_target


def _localize_dense_ic_support(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> dict[str, str]:
    """Gather topology-linked support parts around a dense IC.

    A shelf gives every part enough space but destroys the visual meaning of
    MCU support circuits: a crystal, its load capacitors, reset parts and an
    ICSP header can land in opposite page corners.  This pass uses only final
    connectivity and pin geometry.  Parts sharing an IC pin are placed just
    outside that pin side; no reference names or net-name vocabulary is used.

    Returns a cluster map used by later collision passes so a local support
    part cannot be nudged away from its owner independently.
    """
    from .interfaces import analyze_interfaces

    interfaces = analyze_interfaces(ir, symbols)
    net_of: dict[tuple[str, str], str] = {}
    nodes_of: dict[str, list[tuple[str, str]]] = {}
    for net in ir.nets:
        nodes_of[net.name] = [(r, str(p)) for r, p in net.nodes]
        for node in nodes_of[net.name]:
            net_of[node] = net.name

    clusters: dict[str, str] = {}
    for owner in sorted(ir.components):
        owner_sym = symbols[ir.components[owner].lib_id]
        # Larger MCU/module pin fields need unit/power-domain-aware clustering;
        # applying this compact-board pass to ESP32/64-pin STM32 regressions
        # displaced their established bypass/rail layout. Keep the proven
        # first scope to compact 17..32-pin ICs.
        if owner not in placements or not (16 < len(owner_sym.pins) <= 32):
            continue
        owner_units = placements[owner]
        cid = f"local:{owner}"
        edge_ports: set[str] = set()
        candidates: dict[str, list] = {}
        for pin in owner_sym.pins:
            net_name = net_of.get((owner, pin.number))
            if not net_name:
                continue
            for ref, _pin_no in nodes_of[net_name]:
                if ref == owner or ref not in placements:
                    continue
                sym = symbols[ir.components[ref].lib_id]
                if sym.is_power:
                    continue
                # Local support is deliberately bounded: passives, switches,
                # crystals and connectors. Another IC sharing a bus is a peer,
                # not a satellite of this one.
                if len(sym.pins) > 8 and sym.reference_prefix != "J":
                    continue
                candidates.setdefault(ref, []).append((pin, net_name))

        by_side: dict[tuple[int, int], list[tuple[str, object]]] = {}
        for ref, links in candidates.items():
            # Prefer signal/control pins for reset, oscillator and headers;
            # pure rail bypass capacitors naturally fall back to a power pin.
            signal_links = [
                row for row in links if interfaces[row[1]].kind == "signal"
            ]
            # A programming/debug header sharing several MCU signals is a
            # local support interface. A general I/O header with only UART
            # RX/TX is a board edge port and stays in the global flow; pulling
            # it into the pin field caused RESET/UART crossings and unreadable
            # connector text on the ATmega transcription.
            ref_sym = symbols[ir.components[ref].lib_id]
            if (ref_sym.reference_prefix == "J"
                    and len({net for _pin, net in signal_links}) < 3):
                edge_ports.add(ref)
                continue
            chosen = signal_links or links
            pin = sorted(chosen, key=lambda row: (row[0].unit, row[0].number))[0][0]
            unit = pin.unit if pin.unit in owner_units else next(iter(owner_units))
            direction = pin_outward_dir(owner_units[unit], pin)
            side = (
                int(round(direction[0])), int(round(direction[1]))
            )
            if side == (0, 0):
                side = (-1, 0)
            by_side.setdefault(side, []).append((ref, pin))

        owner_boxes = [
            _body_box(owner_sym, unit, place)
            for unit, place in owner_units.items()
        ]
        left = min(box[0] for box in owner_boxes)
        right = max(box[2] for box in owner_boxes)
        top = min(box[1] for box in owner_boxes)
        bottom = max(box[3] for box in owner_boxes)
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        for side, rows in sorted(by_side.items()):
            rows.sort(key=lambda row: (
                pin_absolute_position(
                    owner_units[
                        row[1].unit if row[1].unit in owner_units else next(iter(owner_units))
                    ], row[1]
                )[1 if side[0] else 0],
                row[0],
            ))
            if side[0] and len(rows) > 4:
                # Dense MCU left/right pin fields commonly have reset,
                # oscillator and analog-reference support on one side. One
                # seven-part column forced an otherwise compact ATmega sheet
                # from A4 to A3. Keep the first control group and last analog
                # support item in the inner column; the contiguous middle
                # group (typically the two-pin oscillator network) occupies
                # the outer column without crossing the last IC pin's route.
                split = (len(rows) + 1) // 2
                inner = rows[:split - 1] + rows[-1:]
                outer = rows[split - 1:-1]
                for column, chunk in enumerate((inner, outer)):
                    chunk_extents = []
                    for ref, _pin in chunk:
                        unit = next(iter(placements[ref]))
                        ex, ey = _unit_extent(symbols[ir.components[ref].lib_id], unit)
                        chunk_extents.append((max(2 * ex, 20.32), max(2 * ey, 15.24)))
                    gap = 5.08
                    height_total = sum(h for _w, h in chunk_extents)
                    height_total += max(0, len(chunk) - 1) * gap
                    cursor_y = max(20.32, center_y - height_total / 2)
                    max_width = max((w for w, _h in chunk_extents), default=20.32)
                    for (ref, _pin), (width, height) in zip(chunk, chunk_extents):
                        outward = 20.32 + column * (max_width + gap)
                        x = (
                            left - width / 2 - outward
                            if side[0] < 0 else right + width / 2 + outward
                        )
                        y = cursor_y + height / 2
                        cursor_y += height + gap
                        unit = next(iter(placements[ref]))
                        placements[ref] = {
                            unit: Placement(_snap(max(20.32, x)), _snap(max(20.32, y)), 0)
                        }
                        clusters[ref] = cid
                continue
            extents = []
            for ref, _pin in rows:
                unit = next(iter(placements[ref]))
                ex, ey = _unit_extent(symbols[ir.components[ref].lib_id], unit)
                extents.append((max(2 * ex, 20.32), max(2 * ey, 15.24)))
            total = sum((h if side[0] else w) for w, h in extents)
            gap = 5.08
            total += max(0, len(rows) - 1) * gap
            cursor = max(
                20.32,
                (center_y - total / 2) if side[0] else (center_x - total / 2),
            )
            for (ref, pin), (width, height) in zip(rows, extents):
                if side[0]:
                    y = cursor + height / 2
                    x = (left - width / 2 - 20.32) if side[0] < 0 else (right + width / 2 + 20.32)
                    cursor += height + gap
                else:
                    x = cursor + width / 2
                    y = (top - height / 2 - 5.08) if side[1] < 0 else (bottom + height / 2 + 5.08)
                    cursor += width + gap
                unit = next(iter(placements[ref]))
                placements[ref] = {
                    unit: Placement(_snap(max(20.32, x)), _snap(max(20.32, y)), 0)
                }
                clusters[ref] = cid
        # Keep general board-edge ports outside the newly formed local
        # columns. Their original shelf slot can otherwise sit exactly under
        # oscillator/reset value text even when symbol bodies do not overlap.
        for index, ref in enumerate(sorted(edge_ports)):
            unit = next(iter(placements[ref]))
            old = placements[ref][unit]
            placements[ref] = {
                unit: Placement(
                    _snap(max(25.4, left - 116.84)),
                    _snap(max(25.4, center_y + index * 35.56)),
                    old.rotation,
                    old.mirror,
                )
            }
        if by_side:
            clusters[owner] = cid
    return clusters


# --- signal-flow layered placement (topology-based, replaces shelf order) ---

def _signal_edges(ir: CircuitIR, symbols: dict[str, SymbolDef], refs: set[str]):
    """Component adjacency over signal nets: [(a, b, directed_a_to_b)].

    Direction comes from pin electrical types: a net's driver-side member
    points toward its non-driver members. Rails (power-symbol nets, ground
    names) carry no flow information and are skipped."""
    from .interfaces import analyze_interfaces

    edges: list[tuple[str, str, bool]] = []
    interfaces = analyze_interfaces(ir, symbols)
    for net in ir.nets:
        interface = interfaces[net.name]
        if interface.kind != "signal":
            continue
        members = [(r, str(p)) for r, p in net.nodes if r in refs]
        if len(members) < 2:
            continue
        drivers = set(interface.drivers) & refs
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
    protected: set[str] | None = None,
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
    protected = protected or set()
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
            and ref not in protected
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
                    # Pins closer than two routing grids leave no room for
                    # their standard 7.62 mm stubs; different-net stubs then
                    # overlap even though the symbol bodies do not.
                    if abs(qx - px) < 2 * GRID and abs(qy - py) < 2 * GRID:
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
        # Moving a satellite relative to a dense pin field requires
        # unit/side-aware clearance, not the small-symbol chain heuristic.
        # Even a two-node BOOT strap can otherwise make the later rigid-body
        # collision pass shift the entire MCU over unrelated power stubs.
        if len(symbols[ir.components[from_ref].lib_id].pins) > 16:
            return
        cid = cluster.get(from_ref, from_ref)
        for _ in range(8):  # chain length guard
            net = net_of.get((ref, pin_no))
            if net is None:
                return
            nodes = net_nodes[net]
            peers = [(r, p) for r, p in nodes if r != ref]
            if len(nodes) == 2:
                nxt = peers[0] if peers else None
            else:
                # A driven output can also be tapped by a connector while one
                # series passive begins the functional chain. When that
                # passive is unique, align it and leave the tap in place.
                src_sym = symbols[ir.components[ref].lib_id]
                src_pin = src_sym.pin(pin_no)
                if len(src_sym.pins) > 16:
                    return
                if src_pin.etype not in {
                    PinType.OUTPUT, PinType.OPENCOLL,
                    PinType.OPENEMIT, PinType.TRISTATE,
                }:
                    return
                movable_peers = [
                    rp for rp in peers if movable(rp[0]) and rp[0] not in moved
                ]
                nxt = movable_peers[0] if len(movable_peers) == 1 else None
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
            chain_gap = _CHAIN_GAP
            target = (
                round(pos[0] + out[0] * chain_gap, 4),
                round(pos[1] + out[1] * chain_gap, 4),
            )
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
    roles, decouple_targets = _classify(ir, symbols)
    placements: dict[str, dict[int, Placement]] = {}

    # Build functional tiles.  Before Component.group existed, board-scale
    # drafts were sorted only by reference and became one enormous vertical
    # strip.  Block ownership is now preserved during merge and each block
    # gets a compact shelf-packed tile.
    grouped: dict[str, list[tuple[str, int]]] = {}
    power_refs: list[str] = []
    for ref, comp in ir.components.items():
        # Power symbols, including block-owned PWR_FLAG annotations, are not
        # functional tile members. They are attached to the real branch in a
        # later pass; shelving a grouped flag is exactly how detached
        # ``PWR_FLAG -> label`` islands were created.
        if symbols[comp.lib_id].is_power:
            power_refs.append(ref)
            continue
        group = comp.group or "CIRCUIT"
        for unit in symbols[comp.lib_id].placed_units():
            grouped.setdefault(group, []).append((ref, unit))

    role_order = {"input": 0, "ic": 1, "decouple": 2, "mid": 3, "output": 4}
    for items in grouped.values():
        items.sort(key=lambda ru: (role_order.get(roles.get(ru[0], "mid"), 3), ru[0], ru[1]))

    # A2/A3 sheets have ample horizontal space. Adaptive tile width allows single-group
    # and medium boards to spread horizontally (target aspect ratio ~1.4) instead of
    # collapsing into a narrow vertical strip.
    SHEET_RIGHT = 570.0
    H_GAP, V_GAP = 7.62, 7.62

    def local_tile(items: list[tuple[str, int]]):
        # Dynamically scale tile width according to part count so 10-20 part circuits
        # distribute evenly across 2-3 wider rows rather than 5-6 narrow rows.
        content_w = min(320.0, max(140.0, len(items) * 16.0)) if len(grouped) <= 2 else 140.0
        local: list[tuple[str, int, float, float]] = []
        x = y = 0.0
        row_h = 0.0
        max_x = 0.0
        for ref, unit in items:
            ex, ey = _unit_extent(symbols[ir.components[ref].lib_id], unit)
            width, height = max(2 * ex, 20.32), max(2 * ey, 15.24)
            if x and x + width > content_w:
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
        # Connectivity layers can collapse many passive/support parts into
        # one enormous column. Preserve the flow order only while it remains
        # a readable tile; otherwise the bounded shelf is more informative.
        if (width > SHEET_RIGHT - 50.8
                or max_h > 240.0
                or (max_h > 2.5 * max(width, 1.0) and width < 60.0)):
            return None  # a flow this tall/narrow reads worse than the shelf
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

    # Re-form MCU support circuits that the global shelf/layering necessarily
    # separated. The returned ownership map is descriptive; unlike a directly
    # aligned two-pin chain, satellites may still move a grid independently
    # when two of their labels/stubs collide inside the local arrangement.
    _localize_dense_ic_support(ir, symbols, placements)

    # Local bypass capacitors belong beside the IC they serve, independent of
    # reference order or the global shelf. Choose the first clear cardinal
    # position; electrical identity came from the power/GND topology above.
    for ref, (target_ref, target_unit) in sorted(decouple_targets.items()):
        if ref not in placements or target_ref not in placements:
            continue
        # Dense MCUs need per-power-unit placement and a larger local routing
        # strategy; moving every bypass around their pin field after the tile
        # is built can create stacked-pin connections. Keep this compact
        # adjustment to small analog/digital ICs.
        if len(symbols[ir.components[target_ref].lib_id].pins) > 16:
            continue
        target = placements[target_ref][target_unit]
        # Prefer the input/left side; output-side timing capacitors commonly
        # occupy the right side of a timer/op-amp and their value text needs
        # more clearance than the body box alone shows.
        candidate_offsets = ((-25.4, 0), (25.4, 0), (0, -25.4), (0, 25.4))
        sym = symbols[ir.components[ref].lib_id]
        unit = next(iter(placements[ref]))
        for dx, dy in candidate_offsets:
            candidate = Placement(_snap(target.x + dx), _snap(target.y + dy), 0)
            box = _body_box(sym, unit, candidate)
            blocked = False
            for other, other_units in placements.items():
                if other == ref:
                    continue
                other_sym = symbols[ir.components[other].lib_id]
                for other_unit, other_place in other_units.items():
                    ob = _body_box(other_sym, other_unit, other_place)
                    if (box[2] > ob[0] and box[0] < ob[2]
                            and box[3] > ob[1] and box[1] < ob[3]):
                        blocked = True
                        break
                if blocked:
                    break
            if not blocked:
                placements[ref] = {unit: candidate}
                break

    # Supply symbols form short horizontal rails around the content instead
    # of another component column. Grounds sit below the content. PWR_FLAGs
    # are attached to a real member's stub in the second pass below.
    rail_x = gnd_x = origin[0]
    top_y = max(20.32, origin[1] - 15.24)
    bottom_y = max_bottom + 7.62
    for ref in sorted(power_refs):
        if roles.get(ref) == "gnd_sym":
            placements.setdefault(ref, {})[1] = Placement(_snap(gnd_x), _snap(bottom_y), 0)
            gnd_x += 20.32
        else:
            placements.setdefault(ref, {})[1] = Placement(_snap(rail_x), _snap(top_y), 0)
            rail_x += 20.32

    # A flag is an ERC annotation, not a separate functional block. Put its
    # pin exactly on the end of a real source/connector stub so the drawing
    # shows what it declares instead of a detached PWR_FLAG island.
    for ref in power_refs:
        is_flag = ir.components[ref].lib_id == "power:PWR_FLAG"
        net = next((n for n in ir.nets if any(r == ref for r, _ in n.nodes)), None)
        anchors = [
            (r, str(pin)) for r, pin in (net.nodes if net else [])
            if r in placements and not symbols[ir.components[r].lib_id].is_power
            and len(symbols[ir.components[r].lib_id].pins) == 2
            and (symbols[ir.components[r].lib_id].reference_prefix in {"J", "BT"}
                 or symbols[ir.components[r].lib_id].is_source)
        ]
        anchors.sort(key=lambda rp: (
            0 if roles.get(rp[0]) == ("input" if is_flag else "output") else 1,
            0 if roles.get(rp[0]) == "ic" else 1,
            rp,
        ))
        # Without an explicit input/source, choosing an arbitrary IC merely
        # moves the ERC annotation into a dense signal pin field.
        anchors = [
            rp for rp in anchors
            if roles.get(rp[0]) == ("input" if is_flag else "output")
        ]
        if not anchors:
            continue
        anchor_ref, anchor_pin_no = anchors[0]
        anchor_pin = symbols[ir.components[anchor_ref].lib_id].pin(anchor_pin_no)
        anchor_units = placements[anchor_ref]
        anchor_unit = anchor_pin.unit if anchor_pin.unit in anchor_units else next(iter(anchor_units))
        target = pin_stub_end(anchor_units[anchor_unit], anchor_pin, 7.62)[1]
        flag_sym = symbols[ir.components[ref].lib_id]
        flag_pin = flag_sym.pin("1")
        zero = pin_absolute_position(Placement(0.0, 0.0, 0), flag_pin)
        placements[ref] = {1: Placement(_snap(target[0] - zero[0]), _snap(target[1] - zero[1]), 0)}

    # Gather series/filter chains into facing rows so the wire router can
    # draw them as real wires (runs before the label-endpoint pass, which
    # remains the final electrical-safety net for anything it nudges).
    clusters = align_chains(ir, symbols, placements, roles)

    # Bring a two-pin output connector to the end of the signal chain it
    # exposes. Layering correctly classifies it as an output, but a later
    # series-chain alignment can move the resistor/load while leaving the
    # connector in its old row. Use the driven signal topology, not names.
    from .interfaces import analyze_interfaces
    interfaces = analyze_interfaces(ir, symbols)
    for ref in sorted(ir.components):
        sym = symbols[ir.components[ref].lib_id]
        if (roles.get(ref) != "output" or sym.reference_prefix != "J"
                or len(sym.pins) != 2 or ref not in placements):
            continue
        signal_net = next((
            net for net in ir.nets
            if interfaces[net.name].kind == "signal"
            and interfaces[net.name].drivers
            and any(r == ref for r, _p in net.nodes)
        ), None)
        if signal_net is None:
            continue
        peers = [r for r, _p in signal_net.nodes if r != ref and r in placements]
        if not peers:
            continue
        peer_boxes = [
            _body_box(symbols[ir.components[r].lib_id], u, p)
            for r in peers for u, p in placements[r].items()
        ]
        peer_pins = []
        for r, pin_no in signal_net.nodes:
            if r == ref or r not in placements:
                continue
            pin = symbols[ir.components[r].lib_id].pin(str(pin_no))
            units = placements[r]
            unit = pin.unit if pin.unit in units else next(iter(units))
            peer_pins.append(pin_absolute_position(units[unit], pin))
        if not peer_pins:
            continue
        y = _snap(sum(p[1] for p in peer_pins) / len(peer_pins))
        x = _snap(max(b[2] for b in peer_boxes) + 15.24)
        unit = next(iter(placements[ref]))
        candidate = Placement(x, y, 0)
        box = _body_box(sym, unit, candidate)
        blocked = any(
            box[2] > ob[0] and box[0] < ob[2]
            and box[3] > ob[1] and box[1] < ob[3]
            for other, units in placements.items() if other != ref
            for other_unit, other_place in units.items()
            for ob in [_body_box(
                symbols[ir.components[other].lib_id], other_unit, other_place
            )]
        )
        if not blocked:
            placements[ref] = {unit: candidate}

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

    # A stub can pass through a nearby pin even when bodies and label
    # endpoints are distinct. KiCad may render this as a misleading overlap;
    # keep a two-grid corridor around every foreign-net pin. Move rigid chain
    # clusters together so improving clearance cannot break a direct chain.
    for _ in range(48):
        pin_rows: list[tuple[str, str, str, tuple[float, float], tuple[float, float]]] = []
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
                start, end = pin_stub_end(units[unit], pin, 7.62)
                pin_rows.append((net.name, ref, str(pin_no), start, end))

        collision = None
        wire_collision = False
        for net, ref, pin_no, start, end in pin_rows:
            for other_net, other_ref, other_pin, point, _other_end in pin_rows:
                if net == other_net or ref == other_ref:
                    continue
                if (min(start[0], end[0]) - .01 <= point[0] <= max(start[0], end[0]) + .01
                        and min(start[1], end[1]) - .01 <= point[1] <= max(start[1], end[1]) + .01
                        and abs((end[0] - start[0]) * (point[1] - start[1])
                                - (end[1] - start[1]) * (point[0] - start[0])) < .02):
                    collision = (ref, other_ref)
                    break
            if collision:
                break
        # Two orthogonal/collinear stubs from different nets can cross
        # between pins without touching either pin point. KiCad then joins
        # the nets even though pin-corridor QA sees nothing (measured on an
        # MCU NRST capacitor crossing a nearby VREF+ stub).
        if collision is None:
            for i, (net, ref, _pin, a, b) in enumerate(pin_rows):
                for other_net, other_ref, _other_pin, c, d in pin_rows[i + 1:]:
                    if net == other_net or ref == other_ref:
                        continue
                    ax0, ax1 = sorted((a[0], b[0]))
                    ay0, ay1 = sorted((a[1], b[1]))
                    cx0, cx1 = sorted((c[0], d[0]))
                    cy0, cy1 = sorted((c[1], d[1]))
                    if (max(ax0, cx0) <= min(ax1, cx1) + .01
                            and max(ay0, cy0) <= min(ay1, cy1) + .01):
                        collision = (ref, other_ref)
                        wire_collision = True
                        break
                if collision:
                    break
        if collision is None:
            break
        candidates = sorted(
            collision,
            key=lambda r: (r not in clusters, roles.get(r) == "ic", r),
        )
        target = candidates[0]
        cid = clusters.get(target)
        move_refs = [r for r in placements if clusters.get(r) == cid] if cid else [target]
        for mr in move_refs:
            placements[mr] = {
                unit: Placement(
                    p.x if wire_collision else _snap(p.x + 2 * GRID),
                    _snap(p.y + 2 * GRID) if wire_collision else p.y,
                    p.rotation,
                    p.mirror,
                )
                for unit, p in placements[mr].items()
            }

    # Collision passes may nudge the zero-body power annotation away from the
    # connector stub it was attached to. Re-establish that exact coordinate
    # last; otherwise KiCad quite correctly reports the apparently touching
    # PWR_FLAG as unconnected.
    for ref in power_refs:
        is_flag = ir.components[ref].lib_id == "power:PWR_FLAG"
        net = next((n for n in ir.nets if any(r == ref for r, _ in n.nodes)), None)
        anchors = [
            (r, str(pin)) for r, pin in (net.nodes if net else [])
            if r in placements and not symbols[ir.components[r].lib_id].is_power
            and len(symbols[ir.components[r].lib_id].pins) == 2
            and (symbols[ir.components[r].lib_id].reference_prefix in {"J", "BT"}
                 or symbols[ir.components[r].lib_id].is_source)
            and roles.get(r) == ("input" if is_flag else "output")
        ]
        anchors.sort()
        if not anchors:
            continue
        anchor_ref, anchor_pin_no = anchors[0]
        anchor_pin = symbols[ir.components[anchor_ref].lib_id].pin(anchor_pin_no)
        anchor_units = placements[anchor_ref]
        anchor_unit = anchor_pin.unit if anchor_pin.unit in anchor_units else next(iter(anchor_units))
        target = pin_stub_end(anchor_units[anchor_unit], anchor_pin, 7.62)[1]
        power_pin = symbols[ir.components[ref].lib_id].pin("1")
        zero = pin_absolute_position(Placement(0.0, 0.0, 0), power_pin)
        placements[ref] = {
            1: Placement(_snap(target[0] - zero[0]), _snap(target[1] - zero[1]), 0)
        }

    return placements
