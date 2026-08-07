"""CircuitIR ↔ JSON conversion and patch application.

The LLM only ever sees/produces the JSON form; these converters are the
boundary where deterministic code takes over.
"""

from __future__ import annotations

from .ir import CircuitIR, Component


def ir_from_json(data: dict) -> CircuitIR:
    ir = CircuitIR(name=data["name"])
    for c in data.get("components", []):
        ir.add(Component(c["ref"], c["lib_id"], c.get("value", ""), c.get("footprint", "")))
    for n in data.get("nets", []):
        ir.connect(n["name"], *[(nd["ref"], str(nd["pin"])) for nd in n["nodes"]])
    ir.nc_pins = [(nc["ref"], str(nc["pin"])) for nc in data.get("nc_pins", [])]
    return ir


def ir_to_json(ir: CircuitIR) -> dict:
    return {
        "name": ir.name,
        "components": [
            {"ref": c.ref, "lib_id": c.lib_id, "value": c.value, "footprint": c.footprint}
            for c in ir.components.values()
        ],
        "nets": [
            {"name": n.name, "nodes": [{"ref": r, "pin": p} for r, p in n.nodes]}
            for n in ir.nets
        ],
        "nc_pins": [{"ref": r, "pin": p} for r, p in ir.nc_pins],
    }


def apply_patch(ir: CircuitIR, ops: list[dict]) -> list[str]:
    """Apply repair ops in place; returns human-readable notes.

    Ops are intentionally coarse domain operations, not raw JSON Patch:
    the model cannot corrupt invariants it does not know about, and every
    op is validated by the next self-ERC pass anyway.
    """
    notes = []
    for op in ops:
        kind = op.get("op")
        ref = op.get("ref", "")
        if kind == "add_component":
            ir.add(Component(ref, op["lib_id"], op.get("value", ""), op.get("footprint", "")))
            notes.append(f"added {ref} ({op['lib_id']})")
        elif kind == "remove_component":
            ir.components.pop(ref, None)
            for net in ir.nets:
                net.nodes = [n for n in net.nodes if n[0] != ref]
            ir.nets = [n for n in ir.nets if n.nodes]
            ir.nc_pins = [n for n in ir.nc_pins if n[0] != ref]
            notes.append(f"removed {ref}")
        elif kind == "connect":
            ir.connect(op["net"], (ref, str(op["pin"])))
            ir.nc_pins = [n for n in ir.nc_pins if n != (ref, str(op["pin"]))]
            notes.append(f"connected {ref}.{op['pin']} to {op['net']}")
        elif kind == "disconnect":
            for net in ir.nets:
                if net.name == op.get("net"):
                    net.nodes = [n for n in net.nodes if n != (ref, str(op["pin"]))]
            ir.nets = [n for n in ir.nets if n.nodes]
            notes.append(f"disconnected {ref}.{op['pin']} from {op.get('net')}")
        elif kind == "set_nc":
            pair = (ref, str(op["pin"]))
            if pair not in ir.nc_pins:
                ir.nc_pins.append(pair)
            notes.append(f"marked {ref}.{op['pin']} NC")
        elif kind == "clear_nc":
            ir.nc_pins = [n for n in ir.nc_pins if n != (ref, str(op["pin"]))]
            notes.append(f"cleared NC on {ref}.{op['pin']}")
        elif kind == "set_value":
            if ref in ir.components:
                ir.components[ref].value = op.get("value", "")
                notes.append(f"set {ref} value {op.get('value')}")
        elif kind == "set_footprint":
            if ref in ir.components:
                ir.components[ref].footprint = op.get("footprint", "")
                notes.append(f"set {ref} footprint")
        else:
            notes.append(f"ignored unknown op {kind!r}")
    return notes
