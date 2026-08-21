"""Deterministic visual QA for generated schematic geometry.

The serving model is text-only, so it cannot honestly inspect the exported
SVG.  This module checks the semantic geometry before emission: symbol/pin
envelopes may not overlap and content must remain inside the largest supported
sheet.  SVG export remains the KiCad rendering oracle; humans can then inspect
the rendered artifact rather than trusting a decoder-only model's claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Placement, pin_absolute_position, pin_stub_end
from .ir import CircuitIR, SymbolDef
from .place import _body_box


@dataclass
class VisualIssue:
    rule: str
    message: str


def _bounds(sym: SymbolDef, unit: int, p: Placement):
    """Approximate BODY box in sheet coordinates.

    Shares the placer's calibration deliberately: this gate must judge
    overlap by the same extents the placer used to avoid it. A private copy
    (byte-identical over 16 symbol/rotation cases) silently coupled the two
    while looking independent.
    """
    return _body_box(sym, unit, p)


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
            if box[0] < 8 or box[1] < 8 or box[2] > 833 or box[3] > 559:
                issues.append(VisualIssue("outside_sheet", f"{ref} unit {unit} exceeds maximum A1 drawing area: {box}"))
    for i, (ra, ua, a) in enumerate(boxes):
        for rb, ub, b in boxes[i + 1 :]:
            # Envelope intersections with positive area are unreadable.
            if min(a[2], b[2]) - max(a[0], b[0]) > 0.2 and min(a[3], b[3]) - max(a[1], b[1]) > 0.2:
                issues.append(VisualIssue("symbol_overlap", f"{ra}.{ua} overlaps {rb}.{ub}"))
    endpoints: dict[tuple[float, float], list[tuple[str, str, str]]] = {}
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
            endpoints.setdefault(end, []).append((net.name, ref, str(pin_no)))
    for point, members in endpoints.items():
        names = {name for name, _, _ in members}
        if len(names) > 1:
            detail = ", ".join(f"{name}:{ref}.{pin}" for name, ref, pin in members)
            issues.append(VisualIssue("label_collision", f"different net labels share {point}: {detail}"))
    return issues


def check_routing(ir, symbols, placements, plan) -> list[VisualIssue]:
    """Detect incorrect geometric connections in the emitted wire plan.

    ERC/netlist round-trip catches the electrical consequence, but this gate
    names the visual cause: a stub or route touching a foreign pin, wires from
    different nets touching, or a detached input power flag. Readability
    measurements such as route length and sheet utilization are deliberately
    not errors: there is no universal electrical threshold for them.
    """
    issues: list[VisualIssue] = []
    node_net = {
        (ref, str(pin)): net.name for net in ir.nets for ref, pin in net.nodes
    }


    def wire_net(tag: str) -> str | None:
        if tag.startswith("net."):
            matches = [n.name for n in ir.nets if tag == f"net.{n.name}" or tag.startswith(f"net.{n.name}.")]
            return max(matches, key=len) if matches else None
        ref, _, pin = tag.rpartition(".")
        return node_net.get((ref, pin))

    pin_points: list[tuple[tuple[float, float], str, str, str]] = []
    for ref, comp in ir.components.items():
        if ref not in placements:
            continue
        sym = symbols[comp.lib_id]
        for pin in sym.pins:
            units = placements[ref]
            if pin.unit in units:
                unit = pin.unit
            elif pin.unit == 0 and len(units) == 1:
                unit = next(iter(units))
            else:
                continue
            pin_points.append((pin_absolute_position(units[unit], pin), node_net.get((ref, pin.number), ""), ref, pin.number))

    def on_segment(point, a, b) -> bool:
        x, y = point
        return (
            min(a[0], b[0]) - .01 <= x <= max(a[0], b[0]) + .01
            and min(a[1], b[1]) - .01 <= y <= max(a[1], b[1]) + .01
            and abs((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])) < .02
        )

    # A PWR_FLAG that exists only through labels is electrically legal but
    # visually detached from the input it claims drives. A flag pin placed on
    # a real member's same-net stub is visibly attached and passes this gate.
    for net in ir.nets:
        flags = [
            (ref, str(pin)) for ref, pin in net.nodes
            if ir.components.get(ref) is not None
            and ir.components[ref].lib_id == "power:PWR_FLAG"
        ]
        has_explicit_input = any(
            ref in ir.components
            and (symbols[ir.components[ref].lib_id].reference_prefix in {"J", "BT"}
                 or symbols[ir.components[ref].lib_id].is_source)
            and len(symbols[ir.components[ref].lib_id].pins) == 2
            for ref, _pin in net.nodes
            if ref in ir.components and ir.components[ref].lib_id in symbols
        )
        if (not flags or not has_explicit_input
                or plan.net_routes.get(net.name) != "stubs"):
            continue
        for ref, pin_no in flags:
            pin = symbols[ir.components[ref].lib_id].pin(pin_no)
            units = placements[ref]
            unit = pin.unit if pin.unit in units else next(iter(units))
            point = pin_absolute_position(units[unit], pin)
            attached = any(
                wire_net(tag) == net.name and not tag.startswith(f"{ref}.")
                and on_segment(point, a, b)
                for a, b, tag in plan.wires
            )
            if not attached:
                issues.append(VisualIssue(
                    "isolated_power_flag",
                    f"{net.name} PWR_FLAG is connected only by label stubs, not to the visible power-input branch",
                ))

    seen = set()
    for a, b, tag in plan.wires:
        net = wire_net(tag)
        if not net:
            continue
        for point, pin_net, ref, pin in pin_points:
            if pin_net != net and on_segment(point, a, b):
                key = (net, ref, pin)
                if key not in seen:
                    seen.add(key)
                    issues.append(VisualIssue(
                        "wire_touches_foreign_pin",
                        f"wire for {net} passes through {ref}.{pin} on {pin_net or 'no net'}",
                    ))

    # Different-net wires short in KiCad when they share an endpoint or when
    # one ends on the other's interior. A pure orthogonal mid-segment crossing
    # (no shared vertex) is legal and does not join the nets — do not flag it.
    def _point_on_open_segment(p, a, b) -> bool:
        (x1, y1), (x2, y2) = a, b
        if abs(x1 - x2) < 0.01:
            return (
                abs(p[0] - x1) < 0.01
                and min(y1, y2) + 0.01 < p[1] < max(y1, y2) - 0.01
            )
        if abs(y1 - y2) < 0.01:
            return (
                abs(p[1] - y1) < 0.01
                and min(x1, x2) + 0.01 < p[0] < max(x1, x2) - 0.01
            )
        return False

    def _collinear_overlap(a, b, c, d) -> bool:
        (x1, y1), (x2, y2) = a, b
        (x3, y3), (x4, y4) = c, d
        if abs(x1 - x2) < 0.01 and abs(x3 - x4) < 0.01 and abs(x1 - x3) < 0.01:
            return max(min(y1, y2), min(y3, y4)) < min(max(y1, y2), max(y3, y4)) - 0.01
        if abs(y1 - y2) < 0.01 and abs(y3 - y4) < 0.01 and abs(y1 - y3) < 0.01:
            return max(min(x1, x2), min(x3, x4)) < min(max(x1, x2), max(x3, x4)) - 0.01
        return False

    for i, (a, b, tag) in enumerate(plan.wires):
        net = wire_net(tag)
        if not net:
            continue
        for c, d, other_tag in plan.wires[i + 1:]:
            other_net = wire_net(other_tag)
            if not other_net or other_net == net:
                continue
            short = _collinear_overlap(a, b, c, d)
            if not short:
                for p in (a, b):
                    if _point_on_open_segment(p, c, d):
                        short = True
                        break
                for p in (c, d):
                    if _point_on_open_segment(p, a, b):
                        short = True
                        break
            if short:
                issues.append(VisualIssue(
                    "wire_crosses_foreign_wire",
                    f"wire for {net} touches wire for {other_net}",
                ))

    return issues
