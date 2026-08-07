"""IR normalization passes that run before ERC / emission."""

from __future__ import annotations

import re

from .ir import CircuitIR, Component, SymbolDef
from .pins import PinType

PWR_FLAG_LIB_ID = "power:PWR_FLAG"

_CS_NET_RE = re.compile(r"(^|_)(CS|SS|NSS|CSN)(_|$|\d)", re.IGNORECASE)


def ensure_bus_pullups(
    ir: CircuitIR, symbols: dict[str, SymbolDef], plus_rail: str | None
) -> list[str]:
    """Add missing bus pull-ups deterministically (knowledge:
    pullup-resistor-sizing — 10k typical).

    Live golden runs showed models reliably wire the buses but omit the
    pull-ups; like PWR_FLAG insertion this is a rule, not a design choice:
    - I2C nets (named SDA/SCL, or carrying pins named so) get 10k to the rail
    - SPI chip-select nets (name matches CS/SS/NSS) get 10k to the rail
    """
    if plus_rail is None or not any(n.name == plus_rail for n in ir.nets):
        return []
    notes: list[str] = []

    def has_pullup(net) -> bool:
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None or sym.reference_prefix != "R" or len(sym.pins) != 2:
                continue
            touched = set()
            for n in ir.nets:
                if any(r == ref for r, _ in n.nodes):
                    touched.add(n.name)
            if net.name in touched and plus_rail in touched:
                return True
        return False

    def pin_names_of(net) -> set[str]:
        out = set()
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None:
                continue
            try:
                out.add((sym.pin(str(pin_no)).name or "").upper())
            except KeyError:
                pass
        return out

    counter = 1

    def next_ref() -> str:
        nonlocal counter
        while f"RPU{counter}" in ir.components:
            counter += 1
        return f"RPU{counter}"

    for net in list(ir.nets):
        if net.name in (plus_rail, "GND") or not net.nodes:
            continue
        names = pin_names_of(net)
        is_i2c = net.name.upper() in ("SDA", "SCL") or any(
            n in ("SDA", "SCL") or n.endswith("/SDA") or n.endswith("/SCL") for n in names
        )
        is_cs = bool(_CS_NET_RE.search(net.name))
        if not (is_i2c or is_cs):
            continue
        if has_pullup(net):
            continue
        ref = next_ref()
        ir.add(Component(ref, "Device:R", "10k"))
        net.nodes.append((ref, "1"))
        ir.connect(plus_rail, (ref, "2"))
        kind = "I2C" if is_i2c else "chip-select"
        notes.append(f"added {kind} pull-up {ref} (10k) on net {net.name} to {plus_rail}")
    return notes


def ensure_pwr_flags(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Add a power:PWR_FLAG to every power net lacking a power_out driver.

    KiCad ERC raises "Input Power pin not driven by any Output Power pins"
    on nets whose only power pins are power_in (all power:* supply symbols
    are power_in) — even when the topology is otherwise perfect. The
    standard fix is one PWR_FLAG (a power_out pin) per such net. Our own
    drive-sufficiency check (erc.py) fails the same way without it, so the
    two ERCs stay in agreement.

    Returns the list of added component refs (the placer must place them).
    PWR_FLAG's symbol definition must be present in `symbols`.
    """
    added: list[str] = []
    counter = 1

    def next_ref() -> str:
        nonlocal counter
        while f"#FLG{counter:02d}" in ir.components:
            counter += 1
        return f"#FLG{counter:02d}"

    for net in ir.nets:
        has_power_in = False
        has_power_out = False
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            if comp is None or comp.lib_id not in symbols:
                continue
            try:
                etype = symbols[comp.lib_id].pin(pin_no).etype
            except KeyError:
                # Unknown pin number: leave it for check_circuit to report as
                # a structured unknown_pin error instead of crashing here.
                continue
            if etype == PinType.PWRIN:
                has_power_in = True
            elif etype == PinType.PWROUT:
                has_power_out = True
        if has_power_in and not has_power_out:
            ref = next_ref()
            ir.add(Component(ref=ref, lib_id=PWR_FLAG_LIB_ID, value="PWR_FLAG"))
            net.nodes.append((ref, "1"))
            added.append(ref)
    return added
