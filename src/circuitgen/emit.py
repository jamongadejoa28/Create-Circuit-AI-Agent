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
routing needed. Direct wires / A* come later per the plan (§7.5).

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

STUB_LEN = 2.54


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


def build_emit_plan(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> EmitPlan:
    """Stub+label geometry for every net node, no_connect markers for NC pins."""
    plan = EmitPlan()
    seen_labels: set[tuple[str, float, float]] = set()
    for net in ir.nets:
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
) -> str:
    placements = normalize_placements(ir, symbols, placements)
    if plan is None:
        plan = build_emit_plan(ir, symbols, placements)

    project = ir.name
    root_uuid = uuid_for(project, "root")
    out: list[str] = []
    w = out.append

    w("(kicad_sch\n")
    w(f"\t(version {SCH_VERSION})\n")
    w(f'\t(generator "{GENERATOR}")\n')
    w(f'\t(generator_version "{GENERATOR_VERSION}")\n')
    w(f'\t(uuid "{root_uuid}")\n')
    w('\t(paper "A4")\n')

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
            f'\t\t(uuid "{uuid_for(project, "root", "junction", _fmt(x), _fmt(y))}")\n\t)\n'
        )

    for x, y in plan.no_connects:
        w(
            f"\t(no_connect\n\t\t(at {_fmt(x)} {_fmt(y)})\n"
            f'\t\t(uuid "{uuid_for(project, "root", "nc", _fmt(x), _fmt(y))}")\n\t)\n'
        )

    for (x1, y1), (x2, y2), tag in plan.wires:
        w(
            f"\t(wire\n\t\t(pts\n\t\t\t(xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})\n\t\t)\n"
            f"\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
            f'\t\t(uuid "{uuid_for(project, "root", "wire", tag)}")\n\t)\n'
        )

    for text, x, y, rot, justify in plan.labels:
        w(
            f'\t(label "{_esc(text)}"\n'
            f"\t\t(at {_fmt(x)} {_fmt(y)} {rot})\n"
            f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
            f"\t\t\t(justify {justify} bottom)\n\t\t)\n"
            f'\t\t(uuid "{uuid_for(project, "root", "label", text, _fmt(x), _fmt(y))}")\n\t)\n'
        )

    # --- placed symbols: one instance block per placed unit (demo ground
    # truth: each unit block repeats the same Reference, carries its own
    # (at)/(unit N)/uuid, and lists ALL pins of the whole symbol) ---
    for ref in sorted(ir.components):
        comp = ir.components[ref]
        sym = symbols[comp.lib_id]
        for unit in sorted(placements[ref]):
            place = placements[ref][unit]
            u = uuid_for(project, "root", ref, "unit", str(unit))
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
                w(f'\t\t(pin "{_esc(pin.number)}"\n\t\t\t(uuid "{uuid_for(project, "root", ref, "unit", str(unit), "pin", tag)}")\n\t\t)\n')
            w(
                f"\t\t(instances\n"
                f'\t\t\t(project "{_esc(project)}"\n'
                f'\t\t\t\t(path "/{root_uuid}"\n'
                f'\t\t\t\t\t(reference "{_esc(ref)}")\n'
                f"\t\t\t\t\t(unit {unit})\n"
                f"\t\t\t\t)\n\t\t\t)\n\t\t)\n"
            )
            w("\t)\n")

    w('\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n')
    w("\t(embedded_fonts no)\n")
    w(")\n")
    return "".join(out)
