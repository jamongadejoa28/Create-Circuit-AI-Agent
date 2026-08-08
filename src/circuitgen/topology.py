"""Domain-neutral topology observations over CircuitIR.

This module does not know STM32, DRV8311, or any benchmark board.  It turns
the netlist into reusable facts that requirements and evaluation contracts can
query: feedback around an amplifier and input/output bypass around a regulator.
Unlike ERC, these checks can detect a circuit that is legal but functionally
incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import CircuitIR, SymbolDef
from .netnames import GROUND_NAMES


@dataclass
class TopologyReport:
    amplifier_total: int = 0
    amplifier_with_feedback: int = 0
    regulator_total: int = 0
    regulator_with_bypass: int = 0
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "amplifier_total": self.amplifier_total,
            "amplifier_with_feedback": self.amplifier_with_feedback,
            "regulator_total": self.regulator_total,
            "regulator_with_bypass": self.regulator_with_bypass,
            "details": self.details,
        }


def _clean(name: str) -> str:
    return name.upper().replace("~", "").replace("{", "").replace("}", "")


def _pin_nets(ir: CircuitIR) -> dict[tuple[str, str], str]:
    return {
        (ref, str(pin)): net.name
        for net in ir.nets
        for ref, pin in net.nodes
    }


def _two_pin_edges(
    ir: CircuitIR, symbols: dict[str, SymbolDef], pin_net: dict[tuple[str, str], str]
) -> dict[str, list[tuple[str, str]]]:
    """net -> [(other_net, ref)] through passive two-pin components."""
    graph: dict[str, list[tuple[str, str]]] = {}
    passive_prefixes = {"R", "C", "L", "FB"}
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or len(sym.pins) != 2 or sym.reference_prefix not in passive_prefixes:
            continue
        nets = [pin_net.get((ref, p.number)) for p in sym.pins]
        if not nets[0] or not nets[1] or nets[0] == nets[1]:
            continue
        graph.setdefault(nets[0], []).append((nets[1], ref))
        graph.setdefault(nets[1], []).append((nets[0], ref))
    return graph


def _path(
    graph: dict[str, list[tuple[str, str]]],
    start: str,
    goal: str,
    max_parts: int = 4,
    blocked_transit: set[str] | None = None,
) -> list[str] | None:
    """BFS start→goal through 2-pin passives.

    ``blocked_transit`` nets (ground/supply rails) may terminate a path but
    never carry it further: output→load-R→GND→bias-R→IN- is NOT feedback.
    """
    if start == goal:
        return []
    blocked = blocked_transit or set()
    todo = [(start, [])]
    seen = {start}
    while todo:
        net, refs = todo.pop(0)
        if len(refs) >= max_parts:
            continue
        for nxt, ref in graph.get(net, []):
            path = refs + [ref]
            if nxt == goal:
                return path
            if nxt not in seen and nxt not in blocked:
                seen.add(nxt)
                todo.append((nxt, path))
    return None


def _named_pin(sym: SymbolDef, names: set[str]):
    return next((p for p in sym.pins if _clean(p.name) in names), None)


def _unique_pin_type(sym: SymbolDef, etype_name: str):
    pins = [p for p in sym.pins if p.etype.name == etype_name]
    return pins[0] if len(pins) == 1 else None


def analyze_topology(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> TopologyReport:
    """Return functional observations without changing or rejecting the IR."""
    report = TopologyReport()
    pin_net = _pin_nets(ir)
    graph = _two_pin_edges(ir, symbols, pin_net)
    # rails must not carry a feedback path: ground-like names plus any net
    # holding a power-symbol pin (matches how the emitter identifies rails)
    rail_nets = {n for n in (net.name for net in ir.nets) if n.upper() in GROUND_NAMES}
    for net in ir.nets:
        for r, _p in net.nodes:
            comp = ir.components.get(r)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym and sym.is_power and comp.lib_id != "power:PWR_FLAG":
                rail_nets.add(net.name)

    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power:
            continue
        # Multi-unit op-amps (LM358/LM324/TL072) carry one amplifier per
        # unit and a separate power unit; analyze each unit independently.
        # Some official KiCad op-amp symbols leave the output pin name blank;
        # its electrical type is still unambiguous.
        units = sorted({p.unit for p in sym.pins if p.unit != 0}) or [1]
        for unit in units:
            unit_pins = [p for p in sym.pins if p.unit in (0, unit)]
            out_pin = next(
                (p for p in unit_pins if _clean(p.name) in {"OUT", "OUTPUT", "VOUT"}), None
            )
            if out_pin is None:
                outputs = [p for p in unit_pins if p.etype.name == "OUTPUT"]
                out_pin = outputs[0] if len(outputs) == 1 else None
            neg_pin = next(
                (p for p in unit_pins if _clean(p.name) in {"-", "IN-", "VIN-", "NEG", "N"}),
                None,
            )
            is_amplifier = (
                "AMPLIFIER_OPERATIONAL" in comp.lib_id.upper()
                or (out_pin is not None and neg_pin is not None
                    and "+" in {_clean(p.name) for p in unit_pins})
            )
            if not (is_amplifier and out_pin and neg_pin):
                continue
            out_net = pin_net.get((ref, out_pin.number))
            neg_net = pin_net.get((ref, neg_pin.number))
            if out_net is None and neg_net is None:
                continue  # completely unused spare unit — not a contract subject
            report.amplifier_total += 1
            feedback = (
                _path(graph, out_net, neg_net, blocked_transit=rail_nets)
                if out_net and neg_net
                else None
            )
            tag = ref if len(units) == 1 else f"{ref}.{unit}"
            if feedback is not None:
                report.amplifier_with_feedback += 1
                report.details.append(f"{tag}: feedback via {feedback or ['direct net']}")
            else:
                report.details.append(f"{tag}: no output-to-inverting-input feedback path")

        is_regulator = "REGULATOR_" in comp.lib_id.upper()
        if not is_regulator:
            continue
        in_pin = _named_pin(sym, {"IN", "VIN", "VI", "INPUT"})
        out_pin = _named_pin(sym, {"OUT", "VOUT", "VO", "OUTPUT"})
        gnd_pin = _named_pin(sym, GROUND_NAMES)
        if not (in_pin and out_pin and gnd_pin):
            continue
        report.regulator_total += 1
        in_net = pin_net.get((ref, in_pin.number))
        out_net = pin_net.get((ref, out_pin.number))
        gnd_net = pin_net.get((ref, gnd_pin.number))
        # ANY single capacitor bridging the rail to ground counts — a bleed
        # resistor in parallel must not mask a real bypass cap (BFS
        # first-edge-wins did exactly that).
        caps = {
            r for r, c in ir.components.items()
            if symbols.get(c.lib_id) and symbols[c.lib_id].reference_prefix == "C"
        }

        def has_cap_bridge(rail_net: str | None) -> bool:
            if not rail_net or not gnd_net:
                return False
            return any(
                nxt == gnd_net and bridging_ref in caps
                for nxt, bridging_ref in graph.get(rail_net, [])
            )

        in_ok, out_ok = has_cap_bridge(in_net), has_cap_bridge(out_net)
        if in_ok and out_ok:
            report.regulator_with_bypass += 1
            report.details.append(f"{ref}: input/output bypass present")
        else:
            missing = [name for name, ok in (("input", in_ok), ("output", out_ok)) if not ok]
            report.details.append(f"{ref}: missing {'/'.join(missing)} bypass capacitor")
    return report
