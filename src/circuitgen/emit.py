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

from .geometry import Placement, pin_outward_dir, pin_stub_end
from .ir import CircuitIR, SymbolDef
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


def _esc(s: str) -> str:
    """Match KiCad's OUTPUTFORMATTER::Quotes (common/richio.cpp): a raw
    newline inside a quoted token makes DSNLEXER reject the entire file."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


@dataclass
class EmitPlan:
    """Resolved drawing primitives — kept so tests can inspect geometry."""

    wires: list[tuple[tuple[float, float], tuple[float, float], str]] = field(default_factory=list)
    labels: list[tuple[str, float, float, int, str]] = field(default_factory=list)  # text,x,y,rot,justify
    junctions: list[tuple[float, float]] = field(default_factory=list)
    no_connects: list[tuple[float, float]] = field(default_factory=list)


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
            rot, justify = _label_orientation(corner[0] - a[0], corner[1] - a[1])
            plan.labels.append((net.name, a[0], a[1], rot, justify))
            continue
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
    if plan is None:
        plan = build_emit_plan(ir, symbols, placements)

    project = project_name or ir.name
    root_uuid = file_uuid or uuid_for(project, ir.name)
    inst_path = instance_path or f"/{root_uuid}"
    global_nets = global_nets or set()
    out: list[str] = []
    w = out.append

    # paper auto-size: content must stay inside the frame (margins + title
    # block reserve) — the 85mm-tall STM32 symbol overflowed A4 (user report)
    max_x = max_y = 0.0
    for ref, units in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        ex = max((abs(p.x) for p in sym.pins), default=5.08) + 10.16
        ey = max((abs(p.y) for p in sym.pins), default=5.08) + 10.16
        for place in units.values():
            max_x = max(max_x, place.x + ex)
            max_y = max(max_y, place.y + ey)
    paper = "A4"
    for cand, w_mm, h_mm in (("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594)):
        paper = cand
        if max_x <= w_mm - 15 and max_y <= h_mm - 30:
            break

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
