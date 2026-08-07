"""Placement engine — Phase 1: simple grid.

Since the Phase-1 emitter connects everything with stub+label, placement
only has to guarantee that symbols (and their stubs/labels) never overlap;
electrical correctness is placement-independent. Layout conventions
(power top / GND bottom / signal flow left→right) and label-density
heuristics arrive in Phase 3 per the plan (§7.5); force-directed ideas
from SKiDL's place.py may inform that later stage.
"""

from __future__ import annotations

from .geometry import GRID, Placement
from .ir import CircuitIR, SymbolDef


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def grid_place(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    columns: int = 4,
    origin: tuple[float, float] = (50.8, 50.8),
    pitch: tuple[float, float] = (30.48, 25.4),
) -> dict[str, dict[int, Placement]]:
    """Deterministic row-major grid, ordinary parts first, then power parts.

    Every placed unit of a multi-unit symbol takes its own grid slot
    (canonical {ref: {unit: Placement}} form).
    """
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
