"""CircuitIR ↔ JSON conversion and patch application.

The LLM only ever sees/produces the JSON form; these converters are the
boundary where deterministic code takes over.
"""

from __future__ import annotations

from .ir import CircuitIR, Component


def ir_from_json(data: dict, notes: list[str] | None = None) -> CircuitIR:
    """Model JSON -> CircuitIR, repairing what only the model can get wrong.

    A duplicate reference used to raise, and the caller turned that into a
    dead stop: measured on a real run, the model wrote BATMON1 twice, the
    block was retried with the identical prompt at temperature 0 so it failed
    identically, and the user got NO schematic at all — for a name collision.
    The first component keeps the reference and the rest are renamed; nets
    stay with the first, so the renamed part arrives unconnected and the
    conduction check reports it, which is a board you can look at instead of
    an error message.
    """
    ir = CircuitIR(name=data["name"])
    seen: set[str] = set()
    for c in data.get("components", []):
        ref = str(c["ref"])
        if ref in seen:
            base = ref.rstrip("0123456789") or ref
            index = 2
            while f"{base}{index}" in seen:
                index += 1
            if notes is not None:
                notes.append(
                    f"duplicate reference {ref} renamed to {base}{index}; its nets "
                    f"stayed with the first {ref}, so the copy arrives unconnected"
                )
            ref = f"{base}{index}"
        seen.add(ref)
        ir.add(Component(
            ref, c["lib_id"], c.get("value", ""), c.get("footprint", ""),
            c.get("group", ""), c.get("binding_error", ""),
        ))
    for n in data.get("nets", []):
        ir.connect(n["name"], *[(nd["ref"], str(nd["pin"])) for nd in n["nodes"]])
    ir.nc_pins = [(nc["ref"], str(nc["pin"])) for nc in data.get("nc_pins", [])]
    return ir


def ir_to_json(ir: CircuitIR) -> dict:
    return {
        "name": ir.name,
        "components": [
            {"ref": c.ref, "lib_id": c.lib_id, "value": c.value, "footprint": c.footprint,
             **({"group": c.group} if c.group else {}),
             **({"binding_error": c.binding_error} if c.binding_error else {})}
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
    op is validated by the next self-ERC pass anyway. A malformed op is
    recorded and skipped — repair must never raise.
    """
    notes = []
    for op in ops:
        try:
            notes.append(_apply_one(ir, op))
        except Exception as e:
            notes.append(f"skipped bad op {op.get('op')!r}: {e}")
    return notes


def _apply_one(ir: CircuitIR, op: dict) -> str:
    kind = op.get("op")
    ref = op.get("ref", "")
    if kind == "add_component":
        if ref in ir.components:
            # models often "add" to mean "fix this component" — treat as replace
            c = ir.components[ref]
            c.lib_id = op.get("lib_id", c.lib_id)
            c.value = op.get("value", c.value)
            c.footprint = op.get("footprint", c.footprint)
            return f"replaced {ref} -> {c.lib_id}"
        ir.add(Component(ref, op["lib_id"], op.get("value", ""), op.get("footprint", "")))
        return f"added {ref} ({op['lib_id']})"
    if kind == "remove_component":
        ir.components.pop(ref, None)
        for net in ir.nets:
            net.nodes = [n for n in net.nodes if n[0] != ref]
        ir.nets = [n for n in ir.nets if n.nodes]
        ir.nc_pins = [n for n in ir.nc_pins if n[0] != ref]
        return f"removed {ref}"
    if kind == "connect":
        pair = (ref, str(op["pin"]))
        # a pin can live in only one net: drop stale memberships first
        for net in ir.nets:
            net.nodes = [n for n in net.nodes if n != pair]
        ir.nets = [n for n in ir.nets if n.nodes or n.name == op["net"]]
        ir.connect(op["net"], pair)
        ir.nc_pins = [n for n in ir.nc_pins if n != pair]
        return f"connected {ref}.{op['pin']} to {op['net']}"
    if kind == "disconnect":
        for net in ir.nets:
            if net.name == op.get("net"):
                net.nodes = [n for n in net.nodes if n != (ref, str(op["pin"]))]
        ir.nets = [n for n in ir.nets if n.nodes]
        return f"disconnected {ref}.{op['pin']} from {op.get('net')}"
    if kind == "set_nc":
        pair = (ref, str(op["pin"]))
        for net in ir.nets:
            net.nodes = [n for n in net.nodes if n != pair]
        ir.nets = [n for n in ir.nets if n.nodes]
        if pair not in ir.nc_pins:
            ir.nc_pins.append(pair)
        return f"marked {ref}.{op['pin']} NC"
    if kind == "clear_nc":
        ir.nc_pins = [n for n in ir.nc_pins if n != (ref, str(op["pin"]))]
        return f"cleared NC on {ref}.{op['pin']}"
    if kind == "set_value":
        if ref in ir.components:
            ir.components[ref].value = op.get("value", "")
            return f"set {ref} value {op.get('value')}"
        return f"set_value: unknown ref {ref}"
    if kind == "set_footprint":
        if ref in ir.components:
            ir.components[ref].footprint = op.get("footprint", "")
            return f"set {ref} footprint"
        return f"set_footprint: unknown ref {ref}"
    return f"ignored unknown op {kind!r}"
