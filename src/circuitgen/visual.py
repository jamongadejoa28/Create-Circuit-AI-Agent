"""Deterministic visual QA for generated schematic geometry.

The serving model is text-only, so it cannot honestly inspect the exported
SVG.  This module checks the semantic geometry before emission: symbol/pin
envelopes may not overlap and content must remain inside the largest supported
sheet.  SVG export remains the KiCad rendering oracle; humans can then inspect
the rendered artifact rather than trusting a decoder-only model's claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Placement, pin_stub_end
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
    # A readable flat sheet should not collapse into a strip.  Require enough
    # symbols that a legitimately linear two/three-part chain is not judged.
    # This is measured on body geometry, independent of paper size or zoom.
    if len(boxes) >= 8:
        min_x = min(b[0] for _r, _u, b in boxes)
        min_y = min(b[1] for _r, _u, b in boxes)
        max_x = max(b[2] for _r, _u, b in boxes)
        max_y = max(b[3] for _r, _u, b in boxes)
        width, height = max_x - min_x, max_y - min_y
        ratio = max(width, height) / max(min(width, height), 0.01)
        # A moderately tall arrangement can still be pathological when it
        # forces a much larger sheet and leaves most of it empty (the ESP32
        # USB-UART draft used 11% of an A2 page). Combine shape and page
        # utilization so ordinary compact portrait blocks are accepted.
        from .emit import fit_paper
        paper, _offset = fit_paper(ir, symbols, placements)
        page_area = {"A4": 297 * 210, "A3": 420 * 297,
                     "A2": 594 * 420, "A1": 841 * 594}[paper]
        utilization = width * height / page_area
        wasteful_strip = len(boxes) >= 12 and ratio > 1.6 and utilization < 0.15
        if ratio > 4.0 or wasteful_strip:
            issues.append(VisualIssue(
                "excessive_aspect_ratio",
                f"{len(boxes)} symbols occupy {width:.1f} x {height:.1f} mm "
                f"(aspect {ratio:.1f}:1, {utilization:.0%} of {paper})",
            ))
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
    """Detect geometric connections and detours in the emitted wire plan.

    ERC/netlist round-trip catches the electrical consequence, but this gate
    names the visual cause: a stub or route touching a foreign pin, wires from
    different nets touching, or a routed tree far longer than its terminals'
    rectilinear minimum.
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
            from .geometry import pin_absolute_position
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
            from .geometry import pin_absolute_position
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

    # A crossing between different-net wires is an electrical junction in
    # KiCad even when no component pin lies at the crossing. This is both a
    # visual failure and a likely hidden short, so report the geometry before
    # the later ERC/netlist oracle merely says that two rails merged.
    for i, (a, b, tag) in enumerate(plan.wires):
        net = wire_net(tag)
        if not net:
            continue
        for c, d, other_tag in plan.wires[i + 1:]:
            other_net = wire_net(other_tag)
            if not other_net or other_net == net:
                continue
            ax0, ax1 = sorted((a[0], b[0]))
            ay0, ay1 = sorted((a[1], b[1]))
            cx0, cx1 = sorted((c[0], d[0]))
            cy0, cy1 = sorted((c[1], d[1]))
            if (max(ax0, cx0) <= min(ax1, cx1) + .01
                    and max(ay0, cy0) <= min(ay1, cy1) + .01):
                issues.append(VisualIssue(
                    "wire_crosses_foreign_wire",
                    f"wire for {net} crosses wire for {other_net}",
                ))

    # Rectilinear bounding span is a conservative lower bound for connecting
    # a net's terminals. Flag only large absolute and relative excesses.
    for net in ir.nets:
        segments = [(a, b) for a, b, tag in plan.wires if wire_net(tag) == net.name]
        if not segments or plan.net_routes.get(net.name) not in {"direct", "l", "tree"}:
            continue
        points = [p for segment in segments for p in segment]
        span = (max(p[0] for p in points) - min(p[0] for p in points)
                + max(p[1] for p in points) - min(p[1] for p in points))
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segments)
        if span > 0 and length - span > 50.0 and length / span > 2.5:
            issues.append(VisualIssue(
                "excessive_wire_detour",
                f"{net} route is {length:.1f} mm for a {span:.1f} mm rectilinear span",
            ))
    return issues
