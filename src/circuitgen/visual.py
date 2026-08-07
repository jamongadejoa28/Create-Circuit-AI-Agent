"""Deterministic visual QA for generated schematic geometry.

The serving model is text-only, so it cannot honestly inspect the exported
SVG.  This module checks the semantic geometry before emission: symbol/pin
envelopes may not overlap and content must remain inside the largest supported
sheet.  SVG export remains the KiCad rendering oracle; humans can then inspect
the rendered artifact rather than trusting a decoder-only model's claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Placement
from .ir import CircuitIR, SymbolDef


@dataclass
class VisualIssue:
    rule: str
    message: str


def _bounds(sym: SymbolDef, unit: int, p: Placement):
    """Approximate BODY box in sheet coordinates.

    Two calibrations against false positives on the (KiCad-verified)
    golden layout: pin envelopes shrink by the pin stick-out — facing
    pins joined by a wire legitimately bring envelopes together, only
    body intersections are unreadable — and the extent axes follow the
    placement rotation (a rot-90 resistor is horizontal, not vertical).
    """
    pins = [x for x in sym.pins if x.unit in (0, unit)] or sym.pins
    stick = max((x.length for x in pins), default=2.54)
    ex = max(max((abs(x.x) for x in pins), default=5.08) - stick, 2.54) + 1.27
    ey = max(max((abs(x.y) for x in pins), default=5.08) - stick, 2.54) + 1.27
    if p.rotation % 180 == 90:
        ex, ey = ey, ex
    return p.x - ex, p.y - ey, p.x + ex, p.y + ey


def check_layout(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> list[VisualIssue]:
    issues: list[VisualIssue] = []
    boxes = []
    for ref, units in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        if sym.is_power:
            continue
        for unit, place in units.items():
            box = _bounds(sym, unit, place)
            boxes.append((ref, unit, box))
            if box[0] < 8 or box[1] < 8 or box[2] > 586 or box[3] > 385:
                issues.append(VisualIssue("outside_sheet", f"{ref} unit {unit} exceeds A2 drawing area: {box}"))
    for i, (ra, ua, a) in enumerate(boxes):
        for rb, ub, b in boxes[i + 1 :]:
            # Envelope intersections with positive area are unreadable.
            if min(a[2], b[2]) - max(a[0], b[0]) > 0.2 and min(a[3], b[3]) - max(a[1], b[1]) > 0.2:
                issues.append(VisualIssue("symbol_overlap", f"{ra}.{ua} overlaps {rb}.{ub}"))
    return issues
