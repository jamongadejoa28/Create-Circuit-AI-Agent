"""Benchmark-only SVG overlay for route diagnostics.

Not part of the product schematic. Writes pin tips, escapes, claimed routes,
junctions, stub fallbacks and structured failure reasons beside a sheet.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from .emit import EmitPlan, STUB_LEN, _instance_unit
from .geometry import Placement, pin_absolute_position, pin_outward_dir, pin_stub_end
from .ir import CircuitIR, SymbolDef

_REASON_COLOR = {
    "terminal_limit": "#b45309",
    "occupied_by_net": "#7c3aed",
    "off_grid_terminal": "#dc2626",
    "escape_blocked": "#ea580c",
    "foreign_geometry": "#c2410c",
    "astar_no_path": "#be123c",
}


def _svg_esc(text: str) -> str:
    return escape(text, {"'": "&apos;", '"': "&quot;"})


def write_route_debug_overlay(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
    plan: EmitPlan,
    path: str | Path,
    *,
    title: str | None = None,
) -> Path:
    """Write a standalone SVG overlay next to benchmark outputs."""
    path = Path(path)
    xs: list[float] = []
    ys: list[float] = []
    pin_marks: list[tuple[float, float, str]] = []
    escape_marks: list[tuple[float, float, float, float]] = []

    for ref, units_map in placements.items():
        comp = ir.components.get(ref)
        if comp is None or comp.lib_id not in symbols:
            continue
        sym = symbols[comp.lib_id]
        for unit, place in units_map.items():
            pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
            for pin in pins:
                px, py = pin_absolute_position(place, pin)
                xs.append(px)
                ys.append(py)
                pin_marks.append((px, py, f"{ref}.{pin.number}"))
                dx, dy = pin_outward_dir(place, pin)
                escape_marks.append((px, py, px + dx * 1.27, py + dy * 1.27))

    for a, b, _tag in plan.wires:
        xs.extend([a[0], b[0]])
        ys.extend([a[1], b[1]])
    for jx, jy in plan.junctions:
        xs.append(jx)
        ys.append(jy)

    if not xs:
        xs, ys = [0.0], [0.0]
    pad = 20.0
    min_x, max_x = min(xs) - pad, max(xs) + pad
    min_y, max_y = min(ys) - pad, max(ys) + pad
    width = max(max_x - min_x, 40.0)
    height = max(max_y - min_y, 40.0)

    def tx(x: float) -> float:
        return x - min_x

    def ty(y: float) -> float:
        return y - min_y

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}" '
        f'height="{height:.2f}" viewBox="0 0 {width:.2f} {height:.2f}">',
        f"<title>{_svg_esc(title or ir.name)} route debug</title>",
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        '<g font-family="DejaVu Sans, sans-serif" font-size="2.2">',
    ]

    # Solid routes
    for a, b, tag in plan.wires:
        if tag.startswith("net."):
            color = "#2563eb"
            width_px = 0.45
        else:
            color = "#94a3b8"
            width_px = 0.3
        lines.append(
            f'<line x1="{tx(a[0]):.2f}" y1="{ty(a[1]):.2f}" '
            f'x2="{tx(b[0]):.2f}" y2="{ty(b[1]):.2f}" '
            f'stroke="{color}" stroke-width="{width_px}"/>'
        )

    for jx, jy in plan.junctions:
        lines.append(
            f'<circle cx="{tx(jx):.2f}" cy="{ty(jy):.2f}" r="0.7" fill="#1d4ed8"/>'
        )

    for px, py, qx, qy in escape_marks:
        lines.append(
            f'<line x1="{tx(px):.2f}" y1="{ty(py):.2f}" '
            f'x2="{tx(qx):.2f}" y2="{ty(qy):.2f}" '
            f'stroke="#16a34a" stroke-width="0.25" stroke-dasharray="0.6 0.6"/>'
        )

    for px, py, label in pin_marks:
        lines.append(
            f'<circle cx="{tx(px):.2f}" cy="{ty(py):.2f}" r="0.45" fill="#0f172a"/>'
        )
        lines.append(
            f'<text x="{tx(px) + 0.8:.2f}" y="{ty(py) - 0.8:.2f}" fill="#334155">'
            f"{_svg_esc(label)}</text>"
        )

    # Stub endpoints for failed nets + failure labels
    y_legend = 4.0
    for net_name, failure in sorted(plan.route_failures.items()):
        color = _REASON_COLOR.get(failure.reason, "#64748b")
        net = next((n for n in ir.nets if n.name == net_name), None)
        if net is not None:
            for ref, pin_no in net.nodes:
                if ref not in placements or ref not in ir.components:
                    continue
                sym = symbols.get(ir.components[ref].lib_id)
                if sym is None:
                    continue
                try:
                    pin = sym.pin(str(pin_no))
                except KeyError:
                    continue
                units_map = placements[ref]
                place = units_map[_instance_unit(pin, units_map, ref)]
                _start, end = pin_stub_end(place, pin, STUB_LEN)
                lines.append(
                    f'<circle cx="{tx(end[0]):.2f}" cy="{ty(end[1]):.2f}" '
                    f'r="0.9" fill="none" stroke="{color}" stroke-width="0.35"/>'
                )
        blockers = ",".join(failure.blocker_nets) if failure.blocker_nets else "-"
        lines.append(
            f'<text x="4" y="{y_legend:.2f}" fill="{color}">'
            f"{_svg_esc(net_name)}: {failure.reason} blockers={_svg_esc(blockers)}"
            f"</text>"
        )
        y_legend += 3.2

    mode_counts = {}
    for mode in plan.net_routes.values():
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    summary = " ".join(f"{k}={v}" for k, v in sorted(mode_counts.items()))
    lines.append(
        f'<text x="4" y="{height - 4:.2f}" fill="#475569">'
        f"routes: {_svg_esc(summary)}</text>"
    )
    lines.append("</g></svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
