"""Functional-sheet partitioning for hierarchical KiCad schematics.

Components have one owning sheet, local nets stay inside it, and cross-sheet
nets become named ports.

**How many sheets.** One sheet per generator group produced twelve sheets for
thirteen parts on a real 4-motor board: a page each for one motor driver, one
encoder, one transceiver, one battery. That is not how a board is drawn. A
peripheral that is a single device hanging off the controller belongs ON the
controller's sheet, next to the pins it connects to; a sheet of its own is
justified when the sub-circuit is substantial enough to be read on its own —
an HDMI driver with two ICs, its own supply conversion and its own crystal.

The measure is the group's device count, read off the library: how many of its
components have more than two pins (a resistor, capacitor, diode or crystal
never counts, a regulator or a transistor does). Two or more devices makes a
sheet; one or none merges into the hub's sheet. The hub is the component with
the most connections on the board — the same arithmetic used when choosing
which duplicate to keep, and on that board it picked the MCU with 22.

The group names themselves are not consulted. The version of this that mapped
"BLDCMOTOR1n" to MOTOR_n and "MCU"/"RESET" to MCU_CAN_DEBUG was a list built
from one benchmark board, which `docs/working-rules.md` §2 says to delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ir import CircuitIR, SymbolDef


@dataclass
class SheetPartition:
    name: str
    groups: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    local_nets: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)


#: a group needs this many multi-pin devices to earn a sheet of its own
SHEET_DEVICE_THRESHOLD = 2


def _sheet_name(group: str) -> str:
    """Stable engineer-facing sheet name from the generator's group id."""
    return re.sub(r"[^A-Z0-9_]+", "_", (group or "MISC").upper()).strip("_") or "MISC"


def _is_device(sym: SymbolDef | None) -> bool:
    """More than a two-terminal passive, by the symbol's own pin count."""
    if sym is None or sym.is_power:
        return False
    return len([p for p in sym.pins if not p.hidden]) > 2


def hub_ref(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> str | None:
    """The component the rest of the board is wired to."""
    connections: dict[str, int] = {}
    for net in ir.nets:
        for ref, _pin in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if comp is None or (sym is not None and sym.is_power):
                continue
            connections[ref] = connections.get(ref, 0) + 1
    if not connections:
        return None
    return max(sorted(connections), key=lambda r: connections[r])


def partition_by_function(
    ir: CircuitIR, symbols: dict[str, SymbolDef] | None = None
) -> dict[str, SheetPartition]:
    """Partition an IR without changing connectivity.

    Global rails and signals spanning two or more sheets become ports in every
    participating sheet. A net is local only when all of its component nodes
    belong to exactly one sheet.
    """
    symbols = symbols or {}
    members: dict[str, list[str]] = {}
    for ref, comp in ir.components.items():
        members.setdefault(comp.group or "MISC", []).append(ref)

    hub = hub_ref(ir, symbols)
    hub_group = (
        (ir.components[hub].group or "MISC") if hub in ir.components else
        next(iter(sorted(members)), "MISC")
    )
    hub_sheet = _sheet_name(hub_group)

    sheet_of_group: dict[str, str] = {}
    for group, refs in members.items():
        devices = sum(_is_device(symbols.get(ir.components[r].lib_id)) for r in refs)
        sheet_of_group[group] = (
            _sheet_name(group) if devices >= SHEET_DEVICE_THRESHOLD else hub_sheet
        )

    sheets: dict[str, SheetPartition] = {}
    owner: dict[str, str] = {}
    for group, refs in members.items():
        name = sheet_of_group[group]
        sheet = sheets.setdefault(name, SheetPartition(name=name))
        sheet.groups.add(group)
        for ref in refs:
            sheet.components.add(ref)
            owner[ref] = name

    for net in ir.nets:
        touched = {owner[ref] for ref, _ in net.nodes if ref in owner}
        if len(touched) == 1:
            sheets[next(iter(touched))].local_nets.add(net.name)
        elif len(touched) > 1:
            for name in touched:
                sheets[name].ports.add(net.name)
    return sheets


def validate_partition(ir: CircuitIR, sheets: dict[str, SheetPartition]) -> list[str]:
    """Return structural errors; an empty list is the hierarchy gate."""
    errors: list[str] = []
    owners: dict[str, list[str]] = {}
    for name, sheet in sheets.items():
        for ref in sheet.components:
            owners.setdefault(ref, []).append(name)
    for ref in ir.components:
        count = len(owners.get(ref, []))
        if count != 1:
            errors.append(f"{ref}: expected one owning sheet, got {count}")
    for net in ir.nets:
        touched = {owners[ref][0] for ref, _ in net.nodes if ref in owners and owners[ref]}
        if len(touched) > 1:
            missing = sorted(name for name in touched if net.name not in sheets[name].ports)
            if missing:
                errors.append(f"{net.name}: missing hierarchy ports in {missing}")
    return errors
