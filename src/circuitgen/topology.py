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
class ConductionReport:
    """Which components are wired so that they can do electrical work."""

    total: int = 0
    working: int = 0
    dead: dict[str, str] = field(default_factory=dict)  # ref -> why

    def as_dict(self) -> dict:
        return {"total": self.total, "working": self.working, "dead": self.dead}


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


def _rail_nets(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> set[str]:
    """Nets held at a potential — where a current path ends, never travels.

    Three ways to be one, in KiCad's own terms: a ground-like name, a leading
    "+" (the convention its power symbols follow: +3V3, +5V, +12V — the same
    test `agent._filter_ops.is_rail` applies), or holding a power symbol.

    The name half is not decoration. A supply that reaches the board through a
    conceptual block instead of a power symbol has no `#PWR` on its net, and
    without it the trace runs straight through one decoupling capacitor into
    its neighbour and reports every cap on the board as GND-to-GND.
    """
    rails = {
        n.name for n in ir.nets
        if n.name.upper() in GROUND_NAMES or n.name.startswith("+")
    }
    for net in ir.nets:
        for ref, _pin in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym and sym.is_power and comp.lib_id != "power:PWR_FLAG":
                rails.add(net.name)
    return rails


def _series_elements(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> set[str]:
    """Refs of two-terminal passives — the parts a current path passes THROUGH.

    Read off the symbol: exactly two visible pins, both electrically PASSIVE.
    That is what a resistor, capacitor, inductor, ferrite, diode, LED or
    mechanical switch has in common, and it comes from the library rather than
    from a list of designators anyone has to maintain. Deliberately wider than
    `_two_pin_edges`, which answers a different question (a bypass/feedback
    BRIDGE, where a diode or a switch is not the same thing as an R or a C).

    Two kinds of two-pin part are NOT an impedance and must not be traced
    through. Both are read off the library, and both were found by a false
    diagnosis on a board that was fine:

    * a CONNECTOR (IEEE 315 designator J) — its pins are separate terminals
      with nothing between them. Treating one as a path merged CANH and CANL
      through the bus header and reported the 120 Ω termination, the one part
      of that board the user could not have placed themselves, as carrying no
      current.
    * a SOURCE — a cell or battery, whose pins are named "+" and "-"
      (Device:Battery, Device:Solar_Cell; a polarised capacitor leaves both
      pin names blank). It HOLDS its terminals apart rather than joining them,
      so a path ends there. Tracing through one collapsed BATTERY_VCC into
      GND and declared the battery, its Schottky and both divider resistors
      dead on a real 4-motor board.

    Two exceptions is the limit. A third means this predicate is the wrong
    one and needs replacing, not extending — a list of designators is exactly
    what `docs/working-rules.md` §2 says to delete.
    """
    out = set()
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or sym.reference_prefix == "J":
            continue
        pins = [p for p in sym.pins if not p.hidden]
        if len(pins) != 2 or not all(p.etype.name == "PASSIVE" for p in pins):
            continue
        if sym.is_source:  # CANDIDATE FIX (scratch): shared definition
            continue
        out.add(ref)
    return out


def analyze_conduction(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> ConductionReport:
    """Is each component wired so that current can flow through it?

    "Is the role present" answers a different question than "is the role doing
    its job", and only the second one is what the user cannot do themselves.
    Measured: a driver_relay board reported role_fulfilment 1.0 with its
    transistor collector on a one-pin net and four invented resistors hanging
    off dead nets. Every part was present; nothing worked.

    Three facts about the finished board, none of which needs to know what the
    part is for:

    * a pin on a net with no other member connects to nothing;
    * a component whose pins all sit on one net is shorted out;
    * a two-terminal part must bridge two DIFFERENT potentials. Its ends are
      traced (through other two-terminal parts only, since those are what a
      current path runs through) until they reach either a rail or a pin of a
      multi-terminal device. Two ends that arrive at the same single endpoint —
      +5V through one resistor and +5V through its neighbour — carry no
      current, whatever ERC says.

    Multi-terminal devices get the first two checks only: whether an IC pin is
    connected to the RIGHT thing is a question about that device, and inventing
    an answer here is how vocabularies get built.
    """
    report = ConductionReport()
    pin_net = _pin_nets(ir)
    rails = _rail_nets(ir, symbols)
    series = _series_elements(ir, symbols)
    net_nodes = {net.name: list(net.nodes) for net in ir.nets}
    nc = {(ref, str(pin)) for ref, pin in ir.nc_pins}

    def endpoints(start: str, without: str) -> set[str]:
        """Potentials reachable from `start` with component `without` removed."""
        found: set[str] = set()
        seen = {start}
        todo = [start]
        while todo:
            name = todo.pop()
            if name in rails:
                found.add(f"rail:{name}")
                continue
            for ref, pin in net_nodes.get(name, []):
                if ref == without:
                    continue
                comp = ir.components.get(ref)
                sym = symbols.get(comp.lib_id) if comp else None
                if sym is not None and sym.is_power:
                    continue  # a bare power symbol is the rail itself
                if ref not in series:
                    found.add(f"{ref}.{pin}")
                    continue
                for other in (symbols[comp.lib_id].pins if comp else []):
                    nxt = pin_net.get((ref, other.number))
                    if nxt and nxt != name and nxt not in seen:
                        seen.add(nxt)
                        todo.append(nxt)
        return found

    for ref, comp in sorted(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        report.total += 1
        pins = [
            p for p in sym.pins
            if not p.hidden and (ref, p.number) not in nc
        ]
        live = {p.number: pin_net.get((ref, p.number)) for p in pins}
        lonely = sorted(
            num for num, name in live.items()
            if name and len(net_nodes.get(name, [])) < 2
        )
        if lonely:
            report.dead[ref] = (
                f"pin {', '.join(lonely)} is the only thing on its net"
            )
            continue
        unwired = sorted(num for num, name in live.items() if not name)
        if unwired:
            report.dead[ref] = f"pin {', '.join(unwired)} is on no net at all"
            continue
        nets = {name for name in live.values() if name}
        if len(nets) < 2:
            report.dead[ref] = (
                f"every pin on {next(iter(nets))} — shorted out" if nets
                else "no pin on any net"
            )
            continue
        if ref in series:
            reach = {name: endpoints(name, ref) for name in nets}
            distinct = {frozenset(v) for v in reach.values() if v}
            if len(distinct) < 2:
                only = sorted(next(iter(distinct))) if distinct else []
                report.dead[ref] = (
                    f"both ends reach the same potential ({', '.join(only)})"
                    if only else "neither end reaches a rail or a device pin"
                )
                continue
        report.working += 1
    return report


def _named_pin(sym: SymbolDef, names: set[str]):
    return next((p for p in sym.pins if _clean(p.name) in names), None)


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
