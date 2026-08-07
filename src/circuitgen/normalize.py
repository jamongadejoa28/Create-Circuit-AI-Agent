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
            owner_group = next(
                (
                    ir.components[r].group
                    for r, _ in net.nodes
                    if r in ir.components and ir.components[r].group
                ),
                "",
            )
            ir.add(Component(ref=ref, lib_id=PWR_FLAG_LIB_ID, value="PWR_FLAG", group=owner_group))
            net.nodes.append((ref, "1"))
            added.append(ref)
    return added


def complete_known_device_pins(
    ir: CircuitIR, symbols: dict[str, SymbolDef], rails: list[str]
) -> list[str]:
    """Complete only pin connections whose electrical meaning is certain.

    This is intentionally a small device rule table, not a generic
    "connect every VDD-looking pin" guess.  It covers the concrete devices
    used by the BLDC benchmark and explicitly marks documented test/unused
    outputs NC.  Configuration and charge-pump pins remain visible ERC
    findings until a datasheet-backed circuit rule is implemented.
    """
    notes: list[str] = []
    rail_set = set(rails)
    logic = "+3V3" if "+3V3" in rail_set else next(
        (r for r in rails if r.startswith("+")), None
    )
    motor = "VBAT" if "VBAT" in rail_set else (
        "+12V" if "+12V" in rail_set else logic
    )

    def connected(ref: str, pin: str) -> bool:
        return any((ref, pin) in n.nodes for n in ir.nets)

    def wire(ref: str, pin: str, net: str | None) -> None:
        if net is None or connected(ref, pin):
            return
        ir.connect(net, (ref, pin))
        if (ref, pin) in ir.nc_pins:
            ir.nc_pins.remove((ref, pin))
        notes.append(f"connected {ref}.{pin} to {net}")

    def nc(ref: str, pin: str) -> None:
        if not connected(ref, pin) and (ref, pin) not in ir.nc_pins:
            ir.nc_pins.append((ref, pin))

    for ref, comp in list(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        lid = comp.lib_id.upper()
        for pin in sym.pins:
            name = pin.name.upper().lstrip("~")
            if "STM32G474" in lid:
                if name in {"VSS", "VSSA"}:
                    wire(ref, pin.number, "GND")
                elif name in {"VDD", "VDDA", "VBAT", "VREF+"}:
                    wire(ref, pin.number, logic)
            elif "DRV8311" in lid:
                if name in {"AGND", "PGND"}:
                    wire(ref, pin.number, "GND")
                elif name in {"VM", "VIN_AVDD"}:
                    wire(ref, pin.number, motor)
            elif "AS5048A" in lid:
                if name == "GND":
                    wire(ref, pin.number, "GND")
                elif name in {"VDD5V", "VDD3V"}:
                    wire(ref, pin.number, logic)
                elif name == "TEST" or name == "PWM":
                    nc(ref, pin.number)
            elif "TJA1051" in lid:
                if name == "GND":
                    wire(ref, pin.number, "GND")
                elif name == "VCC":
                    wire(ref, pin.number, logic)
                elif name == "NC":
                    nc(ref, pin.number)
    return notes


def add_shared_spi_miso_series_resistors(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Isolate encoder MISO outputs on a shared SPI bus with one 47R each.

    Besides matching the requested series resistors, this prevents KiCad's
    output-to-output ERC conflict while preserving the real tri-state bus
    topology.
    """
    notes: list[str] = []
    numeric = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"R(\d+)", r))]
    counter = max(numeric, default=0) + 1
    for net in list(ir.nets):
        if "MISO" not in net.name.upper():
            continue
        targets: list[tuple[str, str]] = []
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None or "AS5048A" not in comp.lib_id.upper():
                continue
            try:
                if sym.pin(pin_no).name.upper() == "MISO":
                    targets.append((ref, pin_no))
            except KeyError:
                continue
        if len(targets) < 2:
            continue
        for ref, pin_no in targets:
            rref = f"R{counter}"
            counter += 1
            group = ir.components[ref].group
            ir.add(Component(rref, "Device:R", "47R", group=group))
            net.nodes.remove((ref, pin_no))
            net.nodes.append((rref, "2"))
            ir.connect(f"{group or ref}_MISO_RAW", (ref, pin_no), (rref, "1"))
            notes.append(f"added {rref} 47R series isolation for {ref} MISO")
    return notes


def ensure_drv8311_vm_decoupling(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Add the requested VM capacitor set inside every DRV8311 channel."""
    notes: list[str] = []
    numeric = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"C(\d+)", r))]
    counter = max(numeric, default=0) + 1

    def pin_net(ref: str, pin: str) -> str | None:
        return next((n.name for n in ir.nets if (ref, pin) in n.nodes), None)

    for ref, comp in list(ir.components.items()):
        if "DRV8311" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        by_name = {p.name.upper(): p.number for p in sym.pins}
        vm = pin_net(ref, by_name.get("VM", ""))
        gnd_pin = by_name.get("PGND") or by_name.get("AGND")
        gnd = pin_net(ref, gnd_pin or "")
        if not vm or not gnd:
            continue
        existing = {
            c.value.upper()
            for c in ir.components.values()
            if c.group == comp.group and c.lib_id == "Device:C"
        }
        for value in ("100nF", "1uF", "10uF", "220uF"):
            if value.upper() in existing:
                continue
            cref = f"C{counter}"
            counter += 1
            ir.add(Component(cref, "Device:C", value, group=comp.group))
            ir.connect(vm, (cref, "1"))
            ir.connect(gnd, (cref, "2"))
            notes.append(f"added {cref} {value} VM decoupling beside {ref}")
    return notes
