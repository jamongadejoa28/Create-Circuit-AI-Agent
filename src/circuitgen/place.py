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
    roles, decouple_target = _classify(ir, symbols)
    placements: dict[str, dict[int, Placement]] = {}

    columns = {"input": origin[0], "mid": origin[0] + 40.64, "ic": origin[0] + 86.36, "output": origin[0] + 137.16}

    def stack(refs_units: list[tuple[str, int]], x: float) -> float:
        """Stack items vertically at column x; returns the lowest y used."""
        y = origin[1]
        for ref, unit in refs_units:
            sym = symbols[ir.components[ref].lib_id]
            _, ey = _unit_extent(sym, unit)
            y += ey
            placements.setdefault(ref, {})[unit] = Placement(x=_snap(x), y=_snap(y), rotation=0)
            y += ey + 7.62
        return y

    max_y = origin[1]
    for role in ("input", "mid", "ic", "output"):
        items = [
            (ref, unit)
            for ref in sorted(ir.components)
            if roles.get(ref) == role
            for unit in symbols[ir.components[ref].lib_id].placed_units()
        ]
        if items:
            max_y = max(max_y, stack(items, columns[role]))

    # decoupling caps: directly beside the IC unit they serve, stacked if several
    per_target: dict[tuple[str, int], int] = {}
    for ref in sorted(r for r, role in roles.items() if role == "decouple"):
        ic, unit = decouple_target[ref]
        ic_sym = symbols[ir.components[ic].lib_id]
        ic_place = placements[ic][unit]
        ex, _ = _unit_extent(ic_sym, unit)
        slot = per_target.get((ic, unit), 0)
        per_target[(ic, unit)] = slot + 1
        placements.setdefault(ref, {})[1] = Placement(
            x=_snap(ic_place.x + ex + 10.16),
            y=_snap(ic_place.y - 5.08 + slot * 15.24),
            rotation=0,
        )

    # power symbols: positive rails in a top row, grounds in a bottom row
    rail_x, gnd_x = columns["input"], columns["input"]
    top_y = origin[1] - 17.78
    bottom_y = max_y + 12.7
    for ref in sorted(r for r, role in roles.items() if role == "rail_sym"):
        placements.setdefault(ref, {})[1] = Placement(x=_snap(rail_x), y=_snap(top_y), rotation=0)
        rail_x += 20.32
    for ref in sorted(r for r, role in roles.items() if role == "gnd_sym"):
        placements.setdefault(ref, {})[1] = Placement(x=_snap(gnd_x), y=_snap(bottom_y), rotation=0)
        gnd_x += 20.32

    return placements
