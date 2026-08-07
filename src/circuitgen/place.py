"""Placement engine.

Two placers share the canonical {ref: {unit: Placement}} output:

- grid_place: deterministic row-major grid (Phase 1 fallback, always safe).
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

from .geometry import GRID, Placement
from .ir import CircuitIR, SymbolDef
from .pins import PinType


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def grid_place(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    columns: int = 4,
    origin: tuple[float, float] = (50.8, 50.8),
    pitch: tuple[float, float] = (30.48, 25.4),
) -> dict[str, dict[int, Placement]]:
    """Deterministic row-major grid, ordinary parts first, then power parts."""
    ordinary = sorted(r for r, c in ir.components.items() if not symbols[c.lib_id].is_power)
    power = sorted(r for r, c in ir.components.items() if symbols[c.lib_id].is_power)

    placements: dict[str, dict[int, Placement]] = {}
    i = 0
    for ref in ordinary + power:
        sym = symbols[ir.components[ref].lib_id]
        for unit in sym.placed_units():
            col, row = i % columns, i // columns
            placements.setdefault(ref, {})[unit] = Placement(
                x=_snap(origin[0] + col * pitch[0]),
                y=_snap(origin[1] + row * pitch[1]),
                rotation=0,
            )
            i += 1
    return placements


# ---------------------------------------------------------------------------


_LABEL_MARGIN = 15.24  # stub (2.54) + label text allowance beyond the pin envelope


def _unit_extent(sym: SymbolDef, unit: int) -> tuple[float, float]:
    """Half-extents (x, y) of a unit's pin envelope, with margin."""
    pins = [p for p in sym.pins if p.unit == unit or p.unit == 0]
    if not pins:
        return (7.62, 7.62)
    ex = max(abs(p.x) for p in pins) + _LABEL_MARGIN
    ey = max(abs(p.y) for p in pins) + 7.62
    return ex, ey


def _classify(ir: CircuitIR, symbols: dict[str, SymbolDef]):
    """Split refs into roles; find decoupling caps and their target ICs."""
    gnd_names = {"GND", "VSS", "AGND", "DGND", "GNDA", "GNDD", "0V"}
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
                            return "gnd" if comp.value in gnd_names else "power"
                    except KeyError:
                        pass
        return "gnd" if name in gnd_names else "signal"

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
            roles[ref] = "gnd_sym" if comp.value in gnd_names else "rail_sym"
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


def heuristic_place(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    origin: tuple[float, float] = (40.64, 55.88),
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

    TILE_CONTENT_W = 145.0
    SHEET_RIGHT = 555.0
    H_GAP, V_GAP = 15.24, 15.24

    def local_tile(items: list[tuple[str, int]]):
        local: list[tuple[str, int, float, float]] = []
        x = y = 0.0
        row_h = 0.0
        max_x = 0.0
        for ref, unit in items:
            ex, ey = _unit_extent(symbols[ir.components[ref].lib_id], unit)
            width, height = max(2 * ex, 25.4), max(2 * ey, 17.78)
            if x and x + width > TILE_CONTENT_W:
                x = 0.0
                y += row_h + 7.62
                row_h = 0.0
            local.append((ref, unit, x + width / 2, y + height / 2))
            x += width + 7.62
            row_h = max(row_h, height)
            max_x = max(max_x, x - 7.62)
        return local, max(max_x, 30.48), y + row_h

    def group_key(name: str):
        upper = name.upper()
        rank = 0 if upper.startswith("POWER") else 1 if upper.startswith("MCU") else 2
        return (rank, upper)

    tile_x, tile_y = origin[0], origin[1]
    row_height = 0.0
    max_bottom = tile_y
    for group in sorted(grouped, key=group_key):
        local, width, height = local_tile(grouped[group])
        # Reserve a heading band even before textual section headings are
        # emitted; this creates the visual whitespace engineers use between
        # repeated channels.
        tile_w = width + 10.16
        tile_h = height + 15.24
        if tile_x > origin[0] and tile_x + tile_w > SHEET_RIGHT:
            tile_x = origin[0]
            tile_y += row_height + V_GAP
            row_height = 0.0
        for ref, unit, lx, ly in local:
            placements.setdefault(ref, {})[unit] = Placement(
                x=_snap(tile_x + 5.08 + lx),
                y=_snap(tile_y + 10.16 + ly),
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
    bottom_y = max_bottom + 12.7
    for ref in sorted(power_refs):
        role = roles.get(ref)
        if role == "gnd_sym":
            placements.setdefault(ref, {})[1] = Placement(_snap(gnd_x), _snap(bottom_y), 0)
            gnd_x += 20.32
        else:
            placements.setdefault(ref, {})[1] = Placement(_snap(rail_x), _snap(top_y), 0)
            rail_x += 20.32

    return placements
