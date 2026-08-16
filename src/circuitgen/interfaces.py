"""Typed electrical interfaces derived from the completed CircuitIR graph.

This is the boundary between semantic connectivity and drawing.  Placement
must not rediscover UART/USB/etc. from prompt words; it consumes topology and
pin electrical types.  Protocol-specific interfaces can later refine these
generic power/signal contracts without changing physical pin identities.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import CircuitIR, SymbolDef
from .netnames import GROUND_NAMES
from .pins import PinType


_DRIVERS = {
    PinType.OUTPUT, PinType.PWROUT, PinType.OPENCOLL,
    PinType.OPENEMIT, PinType.TRISTATE,
}


@dataclass(frozen=True)
class NetInterface:
    name: str
    kind: str  # ground | power | signal
    members: tuple[tuple[str, str], ...]
    drivers: frozenset[str]
    consumers: frozenset[str]
    groups: frozenset[str]


def analyze_interfaces(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> dict[str, NetInterface]:
    out: dict[str, NetInterface] = {}
    for net in ir.nets:
        drivers: set[str] = set()
        consumers: set[str] = set()
        groups: set[str] = set()
        has_power = net.name.upper() in GROUND_NAMES
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if comp and comp.group:
                groups.add(comp.group)
            if sym is None:
                continue
            if sym.is_power:
                has_power = True
            try:
                etype = sym.pin(str(pin_no)).etype
            except KeyError:
                continue
            if etype in _DRIVERS:
                drivers.add(ref)
            else:
                consumers.add(ref)
            if etype in {PinType.PWRIN, PinType.PWROUT}:
                has_power = True
        kind = (
            "ground" if net.name.upper() in GROUND_NAMES
            else "power" if has_power
            else "signal"
        )
        out[net.name] = NetInterface(
            net.name, kind, tuple((r, str(p)) for r, p in net.nodes),
            frozenset(drivers), frozenset(consumers), frozenset(groups),
        )
    return out


def interface_metrics(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> dict[str, object]:
    """Return reviewable aggregate facts without turning them into a score."""
    interfaces = analyze_interfaces(ir, symbols)
    by_kind = {kind: 0 for kind in ("ground", "power", "signal")}
    for interface in interfaces.values():
        by_kind[interface.kind] += 1
    return {
        "typed_nets": len(interfaces),
        "by_kind": by_kind,
        "driven_signal_nets": sum(
            1 for i in interfaces.values() if i.kind == "signal" and i.drivers
        ),
        "cross_group_nets": sum(1 for i in interfaces.values() if len(i.groups) > 1),
    }
