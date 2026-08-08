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
from .sexpr import esc as _esc
from .uuids import uuid_for


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
    """True iff KiCad's netlist matches the IR, by name AND by partition.

    The partition check alone cannot see nets that keep only one real pin
    after power symbols are dropped — e.g. swapped +5V/GND rails still
    yield an identical partition. Since the emitter labels every net with
    its IR name, KiCad's exported net names (modulo the root-sheet "/"
    prefix on local labels) must match too, member-for-member.
    """
    exported = parse_kicad_netlist(exported_netlist)
    # KiCad prefixes sheet-local net names with their sheet path
    # ("/MOTOR_1/M1_SOA_RAW"). IR net names are unique, and a name spanning
    # sheets is emitted as a global label (exported un-prefixed), so mapping
    # to the basename is unambiguous — EXCEPT when KiCad split one IR net
    # into pieces (broken hierarchy): then two exports share a basename, and
    # the extra piece must stay visible under its full path so the mismatch
    # is reported instead of silently merged.
    by_name: dict[str, set[tuple[str, str]]] = {}
    for name, nodes in exported.items():
        key = name.rsplit("/", 1)[-1] or name
        by_name[name if key in by_name else key] = nodes

    msg: list[str] = []

    matched_names: set[str] = set()
    for net in ir.nets:
        want = {(r, str(p)) for r, p in net.nodes if not r.startswith("#")}
        got = by_name.get(net.name, set())
        matched_names.add(net.name)
        if want != got:
            msg.append(
                f"net {net.name!r}: IR has {sorted(want)}, schematic has {sorted(got)}"
            )

    for name, nodes in by_name.items():
        if name in matched_names or not nodes:
            continue
        # KiCad auto-names single dangling/no-connect pins "unconnected-(...)".
        # A pin that SHOULD have been connected already failed its own net's
        # membership check above, so a singleton here is legitimate NC noise.
        if name.startswith("unconnected-") and len(nodes) <= 1:
            continue
        msg.append(f"unexpected net {name!r} in schematic: {sorted(nodes)}")

    want_part = ir_partition(ir)
    got_part = kicad_partition(exported)
    if want_part != got_part:
        missing = want_part - got_part
        extra = got_part - want_part
        if missing:
            msg.append(f"partition missing: {sorted(sorted(n) for n in missing)}")
        if extra:
            msg.append(f"partition extra: {sorted(sorted(n) for n in extra)}")

    if not msg:
        return True, "connectivity identical (by name and by partition)"
    return False, "; ".join(msg)
