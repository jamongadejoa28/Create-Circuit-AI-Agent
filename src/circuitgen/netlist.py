"""Netlist generation and comparison.

The S-expression structure follows SKiDL's tools/kicad10/gen_netlist.py
(MIT License, Copyright (c) Dave Vandenbout):
(export (version "E") (design ...) (components (comp ...)*) (nets (net ...)*)).

Also provides the connectivity round-trip oracle: parse a netlist exported
by `kicad-cli sch export netlist` from a generated .kicad_sch and compare
its net partition against the IR — the strongest automated proof that the
drawn schematic means the circuit we intended (plan §8.3 step 6).
"""

from __future__ import annotations

from pathlib import Path

from simp_sexp import Sexp

from .ir import CircuitIR, SymbolDef
from .pins import PIN_TYPE_TO_KICAD
from .uuids import uuid_for


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def generate_netlist(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> str:
    """Standalone KiCad netlist for the IR (BOM/PCB handoff, debugging)."""
    out: list[str] = []
    w = out.append
    w('(export (version "E")\n')
    w("  (design\n")
    w(f'    (source "{_esc(ir.name)}")\n')
    w('    (tool "circuitgen (0.1.0)"))\n')

    w("  (components\n")
    for ref in sorted(ir.components):
        comp = ir.components[ref]
        lib, _, part = comp.lib_id.partition(":")
        w(f'    (comp (ref "{_esc(ref)}")\n')
        w(f'      (value "{_esc(comp.value)}")\n')
        if comp.footprint:
            w(f'      (footprint "{_esc(comp.footprint)}")\n')
        w(f'      (libsource (lib "{_esc(lib)}") (part "{_esc(part)}"))\n')
        w(f'      (sheetpath (names "/") (tstamps "/"))\n')
        w(f'      (tstamps "{uuid_for(ir.name, "root", ref)}"))\n')
    w("  )\n")

    w("  (nets\n")
    for code, net in enumerate(sorted(ir.nets, key=lambda n: n.name), start=1):
        w(f'    (net (code "{code}") (name "{_esc(net.name)}")\n')
        for ref, pin_no in sorted(net.nodes):
            comp = ir.components[ref]
            etype = symbols[comp.lib_id].pin(pin_no).etype
            w(
                f'      (node (ref "{_esc(ref)}") (pin "{_esc(str(pin_no))}")'
                f' (pintype "{PIN_TYPE_TO_KICAD[etype]}"))\n'
            )
        w("    )\n")
    w("  )\n")
    w(")\n")
    return "".join(out)


def ir_partition(ir: CircuitIR) -> set[frozenset[tuple[str, str]]]:
    """The IR's connectivity as a partition of (ref, pin) nodes.

    `#`-prefixed refs (power symbols, PWR_FLAG) are dropped first — KiCad's
    netlist exporter excludes them, so they can never appear on the oracle
    side. Nets with fewer than two remaining nodes are excluded: a 1-pin
    net carries no connectivity information to compare.
    """
    out = set()
    for net in ir.nets:
        nodes = frozenset(
            (ref, str(p)) for ref, p in net.nodes if not ref.startswith("#")
        )
        if len(nodes) >= 2:
            out.add(nodes)
    return out


def parse_kicad_netlist(path: str | Path) -> dict[str, set[tuple[str, str]]]:
    """Parse a kicad-cli-exported netlist → {net name: {(ref, pin), ...}}."""
    sx = Sexp(Path(path).read_text(encoding="utf-8"))
    nets: dict[str, set[tuple[str, str]]] = {}
    for net in sx.search("/export/nets/net"):
        name = None
        nodes: set[tuple[str, str]] = set()
        for item in net:
            if isinstance(item, list) and item:
                if item[0] == "name":
                    name = str(item[1])
                elif item[0] == "node":
                    ref = pin = None
                    for sub in item:
                        if isinstance(sub, list) and sub:
                            if sub[0] == "ref":
                                ref = str(sub[1])
                            elif sub[0] == "pin":
                                pin = str(sub[1])
                    if ref is not None and pin is not None:
                        nodes.add((ref, pin))
        if name is not None:
            nets[name] = nodes
    return nets


def kicad_partition(
    nets: dict[str, set[tuple[str, str]]]
) -> set[frozenset[tuple[str, str]]]:
    return {frozenset(nodes) for nodes in nets.values() if len(nodes) >= 2}


def compare_connectivity(
    ir: CircuitIR, exported_netlist: str | Path
) -> tuple[bool, str]:
    """True iff the exported netlist's partition equals the IR's."""
    want = ir_partition(ir)
    got = kicad_partition(parse_kicad_netlist(exported_netlist))
    if want == got:
        return True, "connectivity identical"
    missing = want - got
    extra = got - want
    msg = []
    if missing:
        msg.append(f"nets in IR but not in schematic: {sorted(sorted(n) for n in missing)}")
    if extra:
        msg.append(f"nets in schematic but not in IR: {sorted(sorted(n) for n in extra)}")
    return False, "; ".join(msg)
