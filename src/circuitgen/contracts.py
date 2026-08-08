"""Functional topology contracts inferred from normalized requirements."""

from __future__ import annotations

from .ir import CircuitIR, SymbolDef
from .topology import analyze_topology


def infer_contracts(spec: dict) -> list[str]:
    text = " ".join(
        [str(spec.get("summary", ""))]
        + [f"{p.get('role', '')} {p.get('search_query', '')}" for p in spec.get("parts_needed", [])]
        + list(map(str, spec.get("connections_intent", [])))
    ).lower()
    contracts: list[str] = []
    amplifier = any(k in text for k in ("op-amp", "opamp", "operational amplifier", "비반전", "반전 증폭"))
    comparator_only = "comparator" in text and not amplifier
    if amplifier and not comparator_only:
        contracts.append("amplifier_feedback")
    regulator = any(k in text for k in ("voltage regulator", "linear regulator", "ldo", "레귤레이터"))
    if regulator and not any(k in text for k in ("shunt", "reference voltage", "기준 전압")):
        contracts.append("regulator_input_output_bypass")
    return contracts


def contract_instructions(contracts: list[str]) -> list[str]:
    mapping = {
        "amplifier_feedback": (
            "Every operational amplifier must have a real closed-loop path from its "
            "output to its inverting input, directly or through feedback passives."
        ),
        "regulator_input_output_bypass": (
            "Every series voltage regulator must have a capacitor from input to GND "
            "and another capacitor from output to GND."
        ),
    }
    return [mapping[name] for name in contracts if name in mapping]


def validate_contracts(
    ir: CircuitIR, symbols: dict[str, SymbolDef], contracts: list[str]
) -> list[str]:
    topology = analyze_topology(ir, symbols)
    issues: list[str] = []
    if "amplifier_feedback" in contracts:
        if topology.amplifier_total == 0:
            issues.append("amplifier_feedback: no operational amplifier was recognized")
        elif topology.amplifier_with_feedback != topology.amplifier_total:
            issues.append(
                f"amplifier_feedback: {topology.amplifier_with_feedback}/"
                f"{topology.amplifier_total} amplifier(s) have feedback"
            )
    if "regulator_input_output_bypass" in contracts:
        if topology.regulator_total == 0:
            issues.append("regulator_input_output_bypass: no series regulator was recognized")
        elif topology.regulator_with_bypass != topology.regulator_total:
            issues.append(
                f"regulator_input_output_bypass: {topology.regulator_with_bypass}/"
                f"{topology.regulator_total} regulator(s) have both bypass capacitors"
            )
    return issues


def repair_contracts(
    ir: CircuitIR, symbols: dict[str, SymbolDef], spec: dict, contracts: list[str]
) -> list[str]:
    """Deterministically complete explicitly requested, unambiguous topology.

    Values/roles come from the approved requirement spec; pin identities come
    from the selected KiCad symbol.  No part number or benchmark name is used.
    """
    notes: list[str] = []

    def clean(value: str) -> str:
        return value.lower().replace(" ", "").replace("ohm", "").replace("ω", "")

    def net_of(ref: str, pin: str) -> str | None:
        return next((n.name for n in ir.nets if (ref, pin) in n.nodes), None)

    def move(ref: str, pin: str, net_name: str) -> None:
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node != (ref, pin)]
        ir.connect(net_name, (ref, pin))
        ir.nc_pins = [node for node in ir.nc_pins if node != (ref, pin)]

    def requested_value(role_word: str) -> str | None:
        return next(
            (
                clean(str(p.get("value", "")))
                for p in spec.get("parts_needed", [])
                if role_word in str(p.get("role", "")).lower() and p.get("value")
            ),
            None,
        )

    def is_repurposable(ref: str) -> bool:
        """Only rewire parts that do no work yet: every pin unconnected or on
        a single-pin net. Stealing a connected pullup/decoupling part would
        break one topology to satisfy another — the exact ungated-mutation
        failure the repair-op gates exist to prevent."""
        for net in ir.nets:
            if any(r == ref for r, _p in net.nodes) and len(net.nodes) > 1:
                return False
        return True

    if "amplifier_feedback" in contracts:
        wanted = requested_value("feedback")
        # An explicitly value-matched feedback resistor may be re-wired even
        # if connected (it IS the designated part, just wired wrong); without
        # a requested value only dangling resistors may be repurposed.
        resistors = [
            ref for ref, comp in ir.components.items()
            if (sym := symbols.get(comp.lib_id)) is not None
            and sym.reference_prefix == "R" and len(sym.pins) == 2
            and (
                (wanted is not None and clean(comp.value) == wanted)
                or (wanted is None and is_repurposable(ref))
            )
        ]
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None or "AMPLIFIER_OPERATIONAL" not in comp.lib_id.upper():
                continue
            out = _named_or_unique_output(sym)
            neg = next((p for p in sym.pins if p.name.strip().upper() in {"-", "IN-", "VIN-"}), None)
            if not out or not neg or not resistors:
                continue
            out_net = net_of(ref, out.number) or f"{ref}_OUT"
            neg_net = net_of(ref, neg.number) or f"{ref}_INV"
            move(ref, out.number, out_net)
            move(ref, neg.number, neg_net)
            rref = resistors.pop(0)
            rsym = symbols[ir.components[rref].lib_id]
            move(rref, rsym.pins[0].number, out_net)
            move(rref, rsym.pins[1].number, neg_net)
            notes.append(f"contract repair: {rref} wired as {ref} feedback")

    if "regulator_input_output_bypass" in contracts:
        input_value = requested_value("input")
        output_value = requested_value("output")
        caps = [
            ref for ref, comp in ir.components.items()
            if (sym := symbols.get(comp.lib_id)) is not None
            and sym.reference_prefix == "C" and len(sym.pins) == 2
        ]

        def take_cap(value: str | None) -> str | None:
            # a value-matched cap is the designated part (rewirable even if
            # connected); the anonymous fallback may only take a dangling one
            hit = next((r for r in caps if value and clean(ir.components[r].value) == value), None)
            if hit is None:
                hit = next((r for r in caps if is_repurposable(r)), None)
            if hit:
                caps.remove(hit)
            return hit

        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None or "REGULATOR_" not in comp.lib_id.upper():
                continue
            pin_by_name = {p.name.strip().upper(): p for p in sym.pins}
            inp = next((pin_by_name[n] for n in ("IN", "VIN", "VI", "INPUT") if n in pin_by_name), None)
            out = next((pin_by_name[n] for n in ("OUT", "VOUT", "VO", "OUTPUT") if n in pin_by_name), None)
            gnd = next((pin_by_name[n] for n in ("GND", "VSS", "0V") if n in pin_by_name), None)
            if not inp or not out or not gnd:
                continue
            rails = [r.get("name") for r in spec.get("power", {}).get("rails", [])]
            supplies = [r for r in rails if r and r.upper() not in {"GND", "0V", "VSS"}]
            # Under an explicit input->output regulator contract the approved
            # rail order is authoritative. Do not preserve a model's swapped
            # pin-number wiring (measured: GND on +12V and IN on GND).
            in_net = supplies[0] if supplies else (net_of(ref, inp.number) or f"{ref}_VIN")
            out_net = supplies[-1] if len(supplies) > 1 else (net_of(ref, out.number) or f"{ref}_VOUT")
            gnd_net = "GND"
            move(ref, inp.number, in_net)
            move(ref, out.number, out_net)
            move(ref, gnd.number, gnd_net)
            for side, value, rail in (("input", input_value, in_net), ("output", output_value, out_net)):
                cref = take_cap(value)
                if not cref:
                    continue
                csym = symbols[ir.components[cref].lib_id]
                move(cref, csym.pins[0].number, rail)
                move(cref, csym.pins[1].number, gnd_net)
                notes.append(f"contract repair: {cref} wired as {ref} {side} bypass")
    ir.nets = [net for net in ir.nets if net.nodes]
    return notes


def _named_or_unique_output(sym: SymbolDef):
    named = next((p for p in sym.pins if p.name.strip().upper() in {"OUT", "OUTPUT", "VOUT"}), None)
    outputs = [p for p in sym.pins if p.etype.name == "OUTPUT"]
    return named or (outputs[0] if len(outputs) == 1 else None)
