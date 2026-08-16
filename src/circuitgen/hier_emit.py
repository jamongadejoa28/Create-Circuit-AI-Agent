"""Hierarchical schematic emission: SheetPartition → root + child files.

Modeled on the dvk-mx8m-bsb reference the user supplied: one sheet per
functional part, each complete within its own frame; sheets are not
"filled", they are scoped. Cross-sheet connectivity uses global labels
(deliberately not sheet-pin/hierarchical-label pairs — global labels are
positionless, which suits generated schematics, and `single_global_label`
is ignore-by-default in KiCad's ERC config).

Power rails: every sheet that touches a rail gets its own power-symbol
instance (power symbols are project-global by semantics); PWR_FLAGs are
emitted exactly once per rail, on the first owning sheet, to avoid
PWROUT×PWROUT conflicts across sheets.
"""

from __future__ import annotations

from pathlib import Path

from .emit import emit_schematic
from .hierarchy import SheetPartition
from .ir import CircuitIR, Component, SymbolDef
from .normalize import ensure_pwr_flags
from .place import heuristic_place
from .uuids import uuid_for
from .netnames import GROUND_NAMES



def _rail_symbol_lib(rail: str, symbols: dict[str, SymbolDef], parts_index) -> str | None:
    for cand in (f"power:{rail}", f"power:+{rail.lstrip('+')}"):
        if cand in symbols:
            return cand
        if parts_index is not None:
            try:
                parts_index.symbol_source(cand)
                return cand
            except KeyError:
                continue
    return None


def emit_hierarchical(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    partition: dict[str, SheetPartition],
    out_dir: str | Path,
    name: str,
    parts_index=None,
) -> dict:
    """Write <name>.kicad_sch (root) + one child file per sheet.

    Returns {"root": Path, "children": {sheet: Path}, "notes": [...]}.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    root_uuid = uuid_for(name, "hier-root")

    # rails = nets carrying a power symbol in the full IR (before we strip)
    power_refs = {
        r for r, c in ir.components.items()
        if c.lib_id in symbols and symbols[c.lib_id].is_power
    }
    rail_of_net: dict[str, str] = {}
    for net in ir.nets:
        for r, _p in net.nodes:
            if r in power_refs and ir.components[r].lib_id != "power:PWR_FLAG":
                rail_of_net[net.name] = ir.components[r].value

    # biggest sheet first, so the page after the root is the one the board is
    # organised around. Ordering by the name "POWER" was a convention that
    # only applied to boards that happened to have a group called that.
    ordered = sorted(partition.values(), key=lambda s: (-len(s.components), s.name))
    flag_done: set[str] = set()
    children: dict[str, Path] = {}
    page_of: dict[str, int] = {}
    child_ports: dict[str, set[str]] = {}

    for sheet in ordered:
        page_no = len(children) + 2
        comps = [r for r in sheet.components if r not in power_refs]
        if not comps:
            notes.append(f"sheet {sheet.name}: only power symbols — skipped")
            continue
        sub = CircuitIR(name=f"{name}_{sheet.name.lower()}")
        for r in sorted(comps):
            c = ir.components[r]
            sub.add(Component(r, c.lib_id, c.value, c.footprint, c.group, c.binding_error))
        touched_rails: dict[str, str] = {}
        for net in ir.nets:
            nodes = [(r, p) for r, p in net.nodes if r in sub.components]
            if not nodes:
                continue
            sub.connect(net.name, *nodes)
            if net.name in rail_of_net:
                touched_rails[net.name] = rail_of_net[net.name]
        sub.nc_pins = [(r, p) for r, p in ir.nc_pins if r in sub.components]

        # per-sheet power symbols; PWR_FLAG once per rail project-wide
        counter = 1
        for net_name, rail in sorted(touched_rails.items()):
            lib = _rail_symbol_lib(rail, symbols, parts_index)
            if lib is None:
                notes.append(f"sheet {sheet.name}: no power symbol for rail {rail}")
                continue
            if lib not in symbols and parts_index is not None:
                symbols.update(parts_index.load_symbols([lib]))
            ref = f"#PWR{counter:02d}"
            while ref in sub.components:
                counter += 1
                ref = f"#PWR{counter:02d}"
            sub.add(Component(ref, lib, rail))
            sub.connect(net_name, (ref, "1"))
            counter += 1
        pending = set(touched_rails) - flag_done
        if pending:
            ensure_pwr_flags(sub, symbols, only_nets=pending)
            flag_done |= pending

        ports = {p for p in sheet.ports if p not in touched_rails}
        placements = heuristic_place(sub, symbols)
        child_uuid = uuid_for(name, "sheet-file", sheet.name)
        sheet_box_uuid = uuid_for(name, "sheet-box", sheet.name)
        text = emit_schematic(
            sub,
            symbols,
            placements,
            project_name=name,
            file_uuid=child_uuid,
            instance_path=f"/{root_uuid}/{sheet_box_uuid}",
            hier_nets=ports,
            include_sheet_instances=False,
        )
        path = out_dir / f"{sub.name}.kicad_sch"
        path.write_text(text, encoding="utf-8")
        children[sheet.name] = path
        page_of[sheet.name] = page_no
        child_ports[sheet.name] = ports
        notes.append(
            f"sheet {sheet.name}: {len(comps)} components, {len(ports)} ports, page {page_no}"
        )

    # ---- root: a block diagram, not a row of empty rectangles ----
    #
    # The root used to hold nothing but named boxes: no pins, no wires, every
    # name already printed on the sheet it pointed at. A page a reader has to
    # click through and learns nothing from. It now carries the actual
    # interconnect — one sheet pin per net that leaves a sheet, and a wire
    # joining the sheets that share it — which is what a block diagram is for.
    #
    # Sheets are stacked in a column with their pins on the right edge, and
    # each shared net gets its own vertical trunk to the right of the column.
    # A trunk per net means no two nets ever occupy the same x, so the diagram
    # cannot draw a crossing that is not a connection.
    boxes = []
    shown = [s for s in ordered if s.name in children]
    crossing: dict[str, list[str]] = {}
    for sheet in shown:
        # only what the child actually drew as a hierarchical label; rails
        # travel by power symbol and never through a sheet pin
        for port in sorted(child_ports.get(sheet.name, set())):
            crossing.setdefault(port, []).append(sheet.name)
    crossing = {net: names for net, names in crossing.items() if len(names) > 1}

    BOX_W, PIN_PITCH, BOX_GAP = 63.5, 2.54, 12.7
    left, top = 25.4, 25.4

    def mm(v: float) -> float:
        """Snap to the grid and kill float noise.

        A sheet pin at 66.03999999999999 and a wire endpoint computed a
        different way do not meet, and KiCad reports an unconnected endpoint
        that is invisible on screen.
        """
        return round(round(v / 1.27) * 1.27, 3)

    pin_xy: dict[str, list[tuple[float, float]]] = {}
    y = top
    for sheet in shown:
        ports = [p for p in sorted(child_ports.get(sheet.name, set())) if p in crossing]
        h_box = mm(max(25.4, PIN_PITCH * (len(ports) + 1)))
        page_no = page_of[sheet.name]
        sheet_box_uuid = uuid_for(name, "sheet-box", sheet.name)
        fname = children[sheet.name].name
        pins = []
        for index, port in enumerate(ports):
            py = mm(y + PIN_PITCH * (index + 1))
            pin_xy.setdefault(port, []).append((mm(left + BOX_W), py))
            pins.append(
                f'\t\t(pin "{port}" bidirectional\n'
                f"\t\t\t(at {mm(left + BOX_W)} {py} 0)\n"
                "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n"
                "\t\t\t\t(justify right)\n\t\t\t)\n"
                f'\t\t\t(uuid "{uuid_for(name, "sheet-pin", sheet.name, port)}")\n\t\t)\n'
            )
        boxes.append(
            "\t(sheet\n"
            f"\t\t(at {mm(left)} {mm(y)})\n"
            f"\t\t(size {mm(BOX_W)} {h_box})\n"
            "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
            "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n"
            "\t\t(fill\n\t\t\t(color 0 0 0 0.0000)\n\t\t)\n"
            f'\t\t(uuid "{sheet_box_uuid}")\n'
            f'\t\t(property "Sheetname" "{sheet.name}"\n'
            f"\t\t\t(at {left} {y - 0.8} 0)\n"
            "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.524 1.524)\n\t\t\t\t)\n\t\t\t\t(justify left bottom)\n\t\t\t)\n\t\t)\n"
            f'\t\t(property "Sheetfile" "{fname}"\n'
            f"\t\t\t(at {left} {y + h_box + 0.8} 0)\n"
            "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.524 1.524)\n\t\t\t\t)\n\t\t\t\t(justify left top)\n\t\t\t)\n\t\t)\n"
            + "".join(pins)
            + "\t\t(instances\n"
            f'\t\t\t(project "{name}"\n'
            f'\t\t\t\t(path "/{root_uuid}"\n'
            f'\t\t\t\t\t(page "{page_no}")\n'
            "\t\t\t\t)\n\t\t\t)\n\t\t)\n"
            "\t)\n"
        )
        y = mm(y + h_box + BOX_GAP)

    trunk_x = mm(left + BOX_W + 7.62)
    for net in sorted(crossing):
        points = sorted(pin_xy.get(net, []), key=lambda xy: xy[1])
        if len(points) < 2:
            continue
        segs = [((px, py), (trunk_x, py)) for px, py in points]
        segs.append(((trunk_x, points[0][1]), (trunk_x, points[-1][1])))
        # a stub meeting the trunk between its ends is a T, and a T without a
        # junction dot is three wires crossing, not a connection
        for _px, py in points[1:-1]:
            boxes.append(
                "\t(junction\n"
                f"\t\t(at {trunk_x} {py})\n\t\t(diameter 0)\n"
                "\t\t(color 0 0 0 0)\n"
                f'\t\t(uuid "{uuid_for(name, "root-junction", net, str(py))}")\n\t)\n'
            )
        for (x1, y1), (x2, y2) in segs:
            boxes.append(
                "\t(wire\n\t\t(pts\n"
                f"\t\t\t(xy {x1} {y1}) (xy {x2} {y2})\n\t\t)\n"
                "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
                f'\t\t(uuid "{uuid_for(name, "root-wire", net, str(x1), str(y1), str(x2), str(y2))}")\n\t)\n'
            )
        trunk_x = mm(trunk_x + 2.54)
    notes.append(
        f"root: {len(shown)} sheet blocks, {len(crossing)} nets drawn between them"
        if crossing else
        f"root: {len(shown)} sheet blocks; no net crosses between them"
    )

    root_ir = CircuitIR(name=name)
    root_text = emit_schematic(
        root_ir,
        symbols,
        {},
        project_name=name,
        file_uuid=root_uuid,
        extra_body="".join(boxes),
        include_sheet_instances=True,
    )
    root_path = out_dir / f"{name}.kicad_sch"
    root_path.write_text(root_text, encoding="utf-8")
    return {"root": root_path, "children": children, "notes": notes}
