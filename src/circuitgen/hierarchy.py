"""Functional-sheet partitioning for hierarchical KiCad schematics.

This module does not emit KiCad sheets yet.  It defines the deterministic
boundary contract the emitter will consume: components have one owning sheet,
local nets stay inside it, and cross-sheet nets become named sheet ports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ir import CircuitIR


@dataclass
class SheetPartition:
    name: str
    groups: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    local_nets: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)


def _sheet_name(group: str) -> str:
    """Stable engineer-facing sheet name from the generator's group id."""
    upper = (group or "MISC").upper()
    if upper.startswith("POWER"):
        return "POWER"
    if upper.startswith("MCU") or upper.startswith("RESET"):
        return "MCU_CAN_DEBUG"
    motor = re.search(r"(?:BLDC|MOTOR).*?(\d+)$", upper)
    if motor:
        # Instantiation may append an instance digit to an id already ending
        # in 1 (BLDCMOTOR11..14). The final digit is the channel.
        return f"MOTOR_{motor.group(1)[-1]}"
    encoder = re.search(r"ENC(?:ODER)?(\d+)$", upper)
    if encoder:
        return f"ENCODER_{encoder.group(1)[-1]}"
    return re.sub(r"[^A-Z0-9_]+", "_", upper).strip("_") or "MISC"


def partition_by_function(ir: CircuitIR) -> dict[str, SheetPartition]:
    """Partition an IR without changing connectivity.

    Global rails and signals spanning two or more sheets become ports in every
    participating sheet. A net is local only when all of its component nodes
    belong to exactly one sheet.
    """
    sheets: dict[str, SheetPartition] = {}
    owner: dict[str, str] = {}
    for ref, comp in ir.components.items():
        name = _sheet_name(comp.group)
        sheet = sheets.setdefault(name, SheetPartition(name=name))
        sheet.groups.add(comp.group or "MISC")
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
