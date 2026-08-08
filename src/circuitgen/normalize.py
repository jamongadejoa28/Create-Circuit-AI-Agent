"""IR normalization passes that run before ERC / emission."""

from __future__ import annotations

import re

from .ir import CircuitIR, Component, SymbolDef
from .pins import PinType

PWR_FLAG_LIB_ID = "power:PWR_FLAG"

_CS_NET_RE = re.compile(r"(^|_)(CS|SS|NSS|CSN)(_|$|\d)", re.IGNORECASE)


def normalize_common_symbol_aliases(ir: CircuitIR) -> list[str]:
    """Replace vendor-library aliases with loadable KiCad primitives.

    The part index may contain third-party catalog entries whose symbol
    source is not installed in KiCad.  For electrically identical generic
    two-terminal protection parts, retaining an unloadable symbol only makes
    the rendered draft silently drop the part.
    """
    notes: list[str] = []
    for ref, comp in ir.components.items():
        lid = comp.lib_id.lower()
        replacement = None
        if "tvs" in lid and ("diode" in lid or ref.upper().startswith("D")):
            replacement = "Device:D_TVS"
        elif "fuse" in lid and ref.upper().startswith("F"):
            replacement = "Device:Fuse"
        if replacement and comp.lib_id != replacement:
            old = comp.lib_id
            comp.lib_id = replacement
            notes.append(f"{ref}: unavailable vendor alias {old} -> {replacement}")
    return notes


_IF_SIGNALS = {
    "MOSI", "MISO", "SCK", "SCLK", "SDA", "SCL", "TX", "RX", "TXD", "RXD",
    "CANH", "CANL", "CLK", "SWDIO", "SWCLK", "NRST", "INT", "DRDY",
}


def merge_dangling_interface_nets(ir: CircuitIR) -> list[str]:
    """Unify net-name pairs like SPI_MOSI (MCU side) / MOSI (device side).

    Measured failure (BLDC board): blocks name the same interface signal
    with and without a bus prefix, leaving BOTH nets single-pin — self-ERC
    then reports two dangling pins, and the GPIO mapper would attach the
    device net to a fresh pin instead of the matching one. Merge is
    conservative: only when exactly two nets share a known interface
    suffix, at least one is single-pin, and their refs are disjoint.
    """
    notes: list[str] = []

    def suffix(name: str) -> str | None:
        tok = name.rsplit("_", 1)[-1].upper()
        return tok if tok in _IF_SIGNALS else None

    by_suffix: dict[str, list] = {}
    for net in ir.nets:
        s = suffix(net.name)
        if s:
            by_suffix.setdefault(s, []).append(net)
    for s, nets in sorted(by_suffix.items()):
        if len(nets) != 2:
            continue  # >2 same-suffix nets (per-channel buses) stay apart
        a, b = nets
        if min(len(a.nodes), len(b.nodes)) != 1:
            continue
        if {r for r, _ in a.nodes} & {r for r, _ in b.nodes}:
            continue
        keep, drop = (a, b) if (len(a.nodes), len(a.name)) >= (len(b.nodes), len(b.name)) else (b, a)
        keep.nodes.extend(drop.nodes)
        ir.nets.remove(drop)
        notes.append(f"merged dangling interface net {drop.name} into {keep.name}")
    return notes


def enforce_requested_stm32_variant(
    ir: CircuitIR, prompt: str, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Honor an explicit STM32G474RET6 request and migrate nets by pin name."""
    if not re.search(r"STM32G474RE(?:T6)?", prompt, re.I):
        return []
    target_id = "MCU_ST_STM32G4:STM32G474RETx"
    from .symbols import load_symbols

    target = load_symbols([target_id], strict=False).get(target_id)
    if target is None:
        return [f"requested {target_id}, but its KiCad symbol is unavailable"]
    notes: list[str] = []
    for ref, comp in ir.components.items():
        if "STM32G474" not in comp.lib_id.upper() or comp.lib_id == target_id:
            continue
        old = symbols.get(comp.lib_id)
        if old is None:
            continue
        target_by_name: dict[str, list[str]] = {}
        for pin in target.pins:
            target_by_name.setdefault(pin.name.upper(), []).append(pin.number)
        used_by_name: dict[str, int] = {}
        mapping: dict[str, str] = {}
        for pin in old.pins:
            name = pin.name.upper()
            choices = target_by_name.get(name, [])
            if not choices:
                continue
            index = used_by_name.get(name, 0)
            mapping[pin.number] = choices[min(index, len(choices) - 1)]
            used_by_name[name] = index + 1
        for net in ir.nets:
            net.nodes = [
                (r, mapping.get(str(pin), str(pin)) if r == ref else str(pin))
                for r, pin in net.nodes
            ]
        ir.nc_pins = [
            (r, mapping.get(str(pin), str(pin)) if r == ref else str(pin))
            for r, pin in ir.nc_pins
        ]
        old_id = comp.lib_id
        comp.lib_id = target_id
        comp.value = "STM32G474RET6"
        comp.footprint = target.properties.get("Footprint", "")
        notes.append(f"{ref}: {old_id} -> {target_id}; migrated {len(mapping)} pins by name")
    return notes


def sanitize_known_device_nets(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Remove impossible cross-domain wiring and enforce one-net-per-pin.

    Block models sometimes copy the whole interface catalog into unrelated
    blocks.  A power converter pin then acquires SPI/CAN/PWM labels, or a CAN
    transceiver pin simultaneously becomes SPI_MOSI.  Those connections are
    never electrically meaningful and must be removed before ERC/repair.
    """
    notes: list[str] = []
    signal_words = re.compile(r"(^|_)(CAN|SPI|PWM|DIR|EN|RESET)(_|$)", re.I)

    def remove(ref: str, pin: str, net_name: str, why: str) -> None:
        for net in ir.nets:
            if net.name == net_name and (ref, pin) in net.nodes:
                net.nodes = [node for node in net.nodes if node != (ref, pin)]
                notes.append(f"removed impossible {ref}.{pin} from {net_name}: {why}")

    # Power-entry blocks must not consume digital interface nets.
    for ref, comp in list(ir.components.items()):
        if not comp.group.upper().startswith("POWER"):
            continue
        for net in list(ir.nets):
            if signal_words.search(net.name) and any(r == ref for r, _ in net.nodes):
                for r, pin in list(net.nodes):
                    if r == ref:
                        remove(ref, str(pin), net.name, "digital net leaked into power block")

    # TJA1051 pin function is fixed and its names provide a safe whitelist.
    for ref, comp in list(ir.components.items()):
        if "TJA1051" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        allowed = {
            "TXD": ("CAN_TX", "TXD"), "RXD": ("CAN_RX", "RXD"),
            "CANH": ("CANH",), "CANL": ("CANL",),
            "GND": ("GND",), "VCC": ("VCC", "+3V3", "+5V"),
            "S": ("STANDBY", "SILENT", "ENABLE", "GND"),
        }
        for pin in sym.pins:
            pname = pin.name.upper().replace("~", "").replace("{", "").replace("}", "")
            attached = [n.name for n in ir.nets if (ref, pin.number) in n.nodes]
            if pname == "NC":
                for net_name in attached:
                    remove(ref, pin.number, net_name, "TJA1051 NC pin")
                if (ref, pin.number) not in ir.nc_pins:
                    ir.nc_pins.append((ref, pin.number))
                continue
            tokens = allowed.get(pname)
            if tokens:
                for net_name in attached:
                    upper = net_name.upper()
                    if not any(token in upper for token in tokens):
                        remove(ref, pin.number, net_name, f"TJA1051 {pname} whitelist")

    # Final invariant: a physical pin cannot belong to several named nets.
    owners: dict[tuple[str, str], list[str]] = {}
    for net in ir.nets:
        for ref, pin in net.nodes:
            owners.setdefault((ref, str(pin)), []).append(net.name)
    for (ref, pin), names in owners.items():
        unique = list(dict.fromkeys(names))
        if len(unique) <= 1:
            continue
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        try:
            pname = sym.pin(pin).name.upper() if sym else ""
        except KeyError:
            pname = ""

        def score(name: str) -> tuple[int, int]:
            upper = name.upper()
            pin_tokens = set(re.findall(r"[A-Z0-9]+", pname))
            net_tokens = set(re.findall(r"[A-Z0-9]+", upper))
            exact = 20 if pname and (pname == upper or pname in upper) else 0
            rail = 10 if pname in {"VCC", "VDD", "+VOUT"} and any(x in upper for x in ("VCC", "VDD", "+")) else 0
            ground = 20 if "GND" in pname and upper == "GND" else 0
            return exact + rail + ground + len(pin_tokens & net_tokens), -unique.index(name)

        keep = max(unique, key=score)
        for name in unique:
            if name != keep:
                remove(ref, pin, name, f"one-net-per-pin; kept {keep}")
    # A catalog leak can leave a PWR_FLAG as the sole node of its net.
    orphan_flags = set()
    for net in ir.nets:
        if net.nodes and all(
            r in ir.components and ir.components[r].lib_id == PWR_FLAG_LIB_ID
            for r, _ in net.nodes
        ):
            orphan_flags.update(r for r, _ in net.nodes)
            net.nodes.clear()
    for ref in orphan_flags:
        ir.components.pop(ref, None)
        notes.append(f"removed orphan {ref} after interface-net cleanup")
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_dc_power_entry(ir: CircuitIR, output_rail: str = "+12V") -> list[str]:
    """Replace an invalid AC module with a fused DC battery entry circuit."""
    notes: list[str] = []
    ac_refs = [
        r for r, c in ir.components.items()
        if c.group.upper().startswith("POWER") and "CONVERTER_ACDC" in c.lib_id.upper()
    ]
    if not ac_refs:
        return notes
    for ref in ac_refs:
        ir.components.pop(ref, None)
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node[0] != ref]
        ir.nc_pins = [node for node in ir.nc_pins if node[0] != ref]
        notes.append(f"removed {ref}: AC/DC converter is invalid for DC battery input")

    nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"J(\d+)", r))]
    jref = f"J{max(nums, default=0) + 1}"
    ir.add(Component(jref, "Connector_Generic:Conn_01x02", "BATTERY_IN", group="POWER"))
    ir.connect("BATTERY_RAW", (jref, "1"))
    ir.connect("GND", (jref, "2"))

    fuse = next(
        (r for r, c in ir.components.items() if c.lib_id == "Device:Fuse"),
        None,
    )
    if fuse is None:
        nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"F(\d+)", r))]
        fuse = f"F{max(nums, default=0) + 1}"
        ir.add(Component(fuse, "Device:Fuse", "5A", group="POWER"))
    for net in ir.nets:
        net.nodes = [node for node in net.nodes if node[0] != fuse]
    ir.connect("BATTERY_RAW", (fuse, "1"))
    ir.connect(output_rail, (fuse, "2"))

    for ref, comp in ir.components.items():
        if not comp.group.upper().startswith("POWER"):
            continue
        if comp.lib_id == "Device:D_TVS":
            for net in ir.nets:
                net.nodes = [node for node in net.nodes if node[0] != ref]
            ir.connect(output_rail, (ref, "1"))
            ir.connect("GND", (ref, "2"))
        elif comp.lib_id == "Device:C":
            attached = [n for n in ir.nets if any(r == ref for r, _ in n.nodes)]
            if not attached or comp.group.upper().startswith("POWER_REQUIREMENTS"):
                for net in ir.nets:
                    net.nodes = [node for node in net.nodes if node[0] != ref]
                ir.connect(output_rail, (ref, "1"))
                ir.connect("GND", (ref, "2"))
    ir.nets = [n for n in ir.nets if n.nodes]
    notes.append(f"added {jref} -> {fuse} fused DC entry on {output_rail}")
    return notes


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
    referenced = {r for net in ir.nets for r, _ in net.nodes}
    for ref in [
        r for r, c in ir.components.items()
        if c.lib_id == PWR_FLAG_LIB_ID and r not in referenced
    ]:
        ir.components.pop(ref, None)
        added.append(f"removed:{ref}")
    # Remove stale flags when a later normalization pass introduced a real
    # power-output pin on the same net. Keeping both creates KiCad's
    # power-output-to-power-output error.
    remove_flags: set[str] = set()
    for net in ir.nets:
        flags = [r for r, _ in net.nodes if r in ir.components and ir.components[r].lib_id == PWR_FLAG_LIB_ID]
        real_outputs = []
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            if comp is None or ref in flags or comp.lib_id not in symbols:
                continue
            try:
                if symbols[comp.lib_id].pin(pin_no).etype == PinType.PWROUT:
                    real_outputs.append(ref)
            except KeyError:
                pass
        if real_outputs:
            for ref in flags:
                net.nodes = [node for node in net.nodes if node[0] != ref]
                remove_flags.add(ref)
    for ref in remove_flags:
        ir.components.pop(ref, None)
    if remove_flags:
        added.extend(f"removed:{ref}" for ref in sorted(remove_flags))
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
            name = pin.name.upper().replace("~", "").replace("{", "").replace("}", "")
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
            elif "AS5045B" in lid:
                if name == "VSS":
                    wire(ref, pin.number, "GND")
                elif name in {"VDD5V", "VDD3V3"}:
                    wire(ref, pin.number, logic)
                elif name in {"MAGINC", "MAGDEC", "A", "B", "I", "PWM", "NC"}:
                    nc(ref, pin.number)
            elif "TJA1051" in lid:
                if name == "GND":
                    wire(ref, pin.number, "GND")
                elif name == "VCC":
                    wire(ref, pin.number, logic)
                elif name == "TXD":
                    wire(ref, pin.number, "CAN_TX")
                elif name == "RXD":
                    wire(ref, pin.number, "CAN_RX")
                elif name == "CANH":
                    wire(ref, pin.number, "CANH")
                elif name == "CANL":
                    wire(ref, pin.number, "CANL")
                elif name == "NC":
                    nc(ref, pin.number)
                elif name == "S":
                    wire(ref, pin.number, "GND")  # normal mode
        if "STM32G474" in lid:
            # Unused GPIO after interface assignment is intentionally NC.
            # The later system-support pass moves SWD/BOOT/reset pins off NC.
            for pin in sym.pins:
                if pin.etype in {PinType.BIDIR, PinType.INPUT, PinType.OUTPUT}:
                    nc(ref, pin.number)
    return notes


def add_shared_spi_miso_series_resistors(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Isolate encoder MISO/DO outputs on a shared serial bus with 47R each.

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
        # Some templates omit one repeated encoder's data output.  Attach
        # all encoder-group DO/MISO outputs to the declared shared bus before
        # inserting isolation resistors.
        existing_nodes = {(r, p) for n in ir.nets for r, p in n.nodes}
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None or not comp.group.upper().startswith("ENC"):
                continue
            for pin in sym.pins:
                if pin.name.upper() in {"MISO", "DO"} and (ref, pin.number) not in existing_nodes:
                    net.nodes.append((ref, pin.number))
        for ref, pin_no in list(net.nodes):
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None or not comp.group.upper().startswith("ENC"):
                continue
            try:
                if sym.pin(pin_no).name.upper() in {"MISO", "DO"}:
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


def ensure_drv8311h_operating_network(
    ir: CircuitIR, symbols: dict[str, SymbolDef], logic_rail: str = "+3V3"
) -> list[str]:
    """Complete the TI-documented DRV8311H 3x-PWM support network."""
    notes: list[str] = []
    counters: dict[str, int] = {}

    def next_ref(prefix: str) -> str:
        if prefix not in counters:
            nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(prefix + r"(\d+)", r))]
            counters[prefix] = max(nums, default=0) + 1
        ref = f"{prefix}{counters[prefix]}"
        counters[prefix] += 1
        return ref

    def move(ref: str, pin: str, net_name: str) -> None:
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node != (ref, pin)]
        ir.connect(net_name, (ref, pin))
        ir.nc_pins = [node for node in ir.nc_pins if node != (ref, pin)]

    drivers = [(r, c) for r, c in ir.components.items() if "DRV8311H" in c.lib_id.upper()]
    for channel, (ref, comp) in enumerate(sorted(drivers), 1):
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        pins = {
            p.name.upper().replace("~", "").replace("{", "").replace("}", ""): p.number
            for p in sym.pins
        }
        vm_net = next((n.name for n in ir.nets if (ref, pins.get("VM", "")) in n.nodes), "+12V")

        # MODE Hi-Z selects 3x PWM. Low-side inputs are ignored in this mode
        # and held low; GAIN/SLEW use documented ground settings.
        for name in ("INLA", "INLB", "INLC", "GAIN", "SLEW"):
            if name in pins:
                move(ref, pins[name], "GND")
        if "MODE" in pins:
            for net in ir.nets:
                net.nodes = [node for node in net.nodes if node != (ref, pins["MODE"])]
            if (ref, pins["MODE"]) not in ir.nc_pins:
                ir.nc_pins.append((ref, pins["MODE"]))
        if "SLEEP" in pins:
            move(ref, pins["SLEEP"], logic_rail)

        def add_part(lib_id: str, value: str, a: str, b: str, prefix: str) -> str:
            part_ref = next_ref(prefix)
            ir.add(Component(part_ref, lib_id, value, group=comp.group))
            ir.connect(a, (part_ref, "1"))
            ir.connect(b, (part_ref, "2"))
            return part_ref

        if "CP" in pins:
            cp_net = f"M{channel}_CP"
            move(ref, pins["CP"], cp_net)
            c = add_part("Device:C", "100nF", cp_net, vm_net, "C")
            notes.append(f"added {c} 100nF CP-to-VM for {ref}")
        if "CSAREF" in pins:
            csa_net = f"M{channel}_CSAREF"
            move(ref, pins["CSAREF"], csa_net)
            c = add_part("Device:C", "100nF", csa_net, "GND", "C")
            notes.append(f"added {c} 100nF CSAREF bypass for {ref}")
        if "AVDD" in pins:
            avdd_net = f"M{channel}_AVDD"
            move(ref, pins["AVDD"], avdd_net)
            c = add_part("Device:C", "1uF", avdd_net, "GND", "C")
            if "FAULT" in pins:
                fault_net = f"M{channel}_FAULT"
                move(ref, pins["FAULT"], fault_net)
                r = add_part("Device:R", "5.1k", avdd_net, fault_net, "R")
                notes.append(f"added {r} nFAULT pull-up for {ref}")

        jref = next_ref("J")
        ir.add(Component(jref, "Connector_Generic:Conn_01x03", f"MOTOR_{channel}", group=comp.group))
        for index, out_name in enumerate(("OUTA", "OUTB", "OUTC"), 1):
            if out_name in pins:
                net_name = f"M{channel}_{out_name}"
                move(ref, pins[out_name], net_name)
                ir.connect(net_name, (jref, str(index)))
        notes.append(f"added {jref} three-phase output connector for {ref}")

    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_canfd_bus_protection(ir: CircuitIR) -> list[str]:
    """Add CANH/CANL connector, selectable 120R termination and TVS parts."""
    if not any("TJA1051" in c.lib_id.upper() for c in ir.components.values()):
        return []
    if any(c.value == "CAN_FD" for c in ir.components.values()):
        return []
    notes: list[str] = []

    def next_ref(prefix: str) -> str:
        nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(prefix + r"(\d+)", r))]
        return f"{prefix}{max(nums, default=0) + 1}"

    jref = next_ref("J")
    ir.add(Component(jref, "Connector_Generic:Conn_01x03", "CAN_FD", group="MCU"))
    ir.connect("CANH", (jref, "1"))
    ir.connect("CANL", (jref, "2"))
    ir.connect("GND", (jref, "3"))

    rref = next_ref("R")
    ir.add(Component(rref, "Device:R", "120R", group="MCU"))
    ir.connect("CANH", (rref, "1"))
    ir.connect("CAN_TERM", (rref, "2"))
    jp = next_ref("JP")
    ir.add(Component(jp, "Jumper:Jumper_2_Open", "CAN_TERM_ENABLE", group="MCU"))
    ir.connect("CAN_TERM", (jp, "1"))
    ir.connect("CANL", (jp, "2"))

    for net_name in ("CANH", "CANL"):
        dref = next_ref("D")
        ir.add(Component(dref, "Device:D_TVS", "CAN_ESD_TVS", group="MCU"))
        ir.connect(net_name, (dref, "1"))
        ir.connect("GND", (dref, "2"))
    notes.append(f"added {jref} CAN-FD connector, selectable 120R termination and dual TVS")
    return notes


def apply_stm32g474ret6_foc_pinmap(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Wire the fixed, conflict-free 4-axis FOC map for STM32G474RETx.

    HRTIM A-F provide twelve 3x-PWM outputs, twelve analog-capable GPIOs
    receive SOA/B/C through 47R/1nF filters, SPI1 serves four encoders, and
    FDCAN2 uses PB5/PB6 so PA11 remains available to HRTIM1_CHB2.
    """
    notes: list[str] = []
    mcu_ref = next(
        (r for r, c in ir.components.items() if "STM32G474RE" in c.lib_id.upper()),
        None,
    )
    if mcu_ref is None:
        return notes
    mcu_sym = symbols.get(ir.components[mcu_ref].lib_id)
    if mcu_sym is None:
        return notes
    mcu_pins = {p.name.upper(): p.number for p in mcu_sym.pins}

    def move(ref: str, pin: str, net_name: str) -> None:
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node != (ref, pin)]
        ir.connect(net_name, (ref, pin))
        ir.nc_pins = [node for node in ir.nc_pins if node != (ref, pin)]

    def next_ref(prefix: str) -> str:
        nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(prefix + r"(\d+)", r))]
        return f"{prefix}{max(nums, default=0) + 1}"

    drivers = sorted(
        (r for r, c in ir.components.items() if "DRV8311H" in c.lib_id.upper()),
        key=lambda r: int(re.search(r"(\d+)$", r).group(1)) if re.search(r"(\d+)$", r) else r,
    )
    pwm_gpio = [
        "PA8", "PA9", "PA10", "PA11", "PB12", "PB13",
        "PB14", "PB15", "PC8", "PC9", "PC6", "PC7",
    ]
    adc_gpio = [
        "PC0", "PC1", "PC2", "PC3", "PC4", "PC5",
        "PA0", "PA1", "PA2", "PA3", "PB0", "PB1",
    ]
    pwm_index = adc_index = 0
    for channel, ref in enumerate(drivers, 1):
        sym = symbols.get(ir.components[ref].lib_id)
        if sym is None:
            continue
        dpins = {
            p.name.upper().replace("~", "").replace("{", "").replace("}", ""): p.number
            for p in sym.pins
        }
        for phase, input_name in zip("ABC", ("INHA", "INHB", "INHC")):
            if pwm_index >= len(pwm_gpio) or input_name not in dpins:
                continue
            net_name = f"PWM_{phase}{channel}"
            gpio = pwm_gpio[pwm_index]
            pwm_index += 1
            move(ref, dpins[input_name], net_name)
            move(mcu_ref, mcu_pins[gpio], net_name)
            notes.append(f"{net_name}: {mcu_ref}.{gpio} -> {ref}.{input_name}")

        for sense_name in ("SOA", "SOB", "SOC"):
            if adc_index >= len(adc_gpio) or sense_name not in dpins:
                continue
            gpio = adc_gpio[adc_index]
            adc_index += 1
            raw = f"M{channel}_{sense_name}_RAW"
            filtered = f"M{channel}_{sense_name}_ADC"
            move(ref, dpins[sense_name], raw)
            move(mcu_ref, mcu_pins[gpio], filtered)
            existing_r = next(
                (
                    r for r, c in ir.components.items()
                    if c.group == ir.components[ref].group and c.lib_id == "Device:R"
                    and c.value.upper() == "47R"
                    and {raw, filtered} <= {
                        n.name for n in ir.nets if any(rr == r for rr, _ in n.nodes)
                    }
                ),
                None,
            )
            if existing_r is None:
                rref = next_ref("R")
                ir.add(Component(rref, "Device:R", "47R", group=ir.components[ref].group))
                ir.connect(raw, (rref, "1"))
                ir.connect(filtered, (rref, "2"))
                cref = next_ref("C")
                ir.add(Component(cref, "Device:C", "1nF", group=ir.components[ref].group))
                ir.connect(filtered, (cref, "1"))
                ir.connect("GND", (cref, "2"))
                notes.append(f"{filtered}: {sense_name} -> 47R/1nF -> {mcu_ref}.{gpio}")

    # SPI1 AF5: PA5=SCK, PA6=MISO, PA7=MOSI.
    for gpio, net_name in (("PA5", "SPI_SCK"), ("PA6", "SPI_MISO"), ("PA7", "SPI_MOSI")):
        move(mcu_ref, mcu_pins[gpio], net_name)
    encoders = sorted(
        (r for r, c in ir.components.items() if c.group.upper().startswith("ENC")),
        key=lambda r: int(re.search(r"(\d+)$", r).group(1)) if re.search(r"(\d+)$", r) else r,
    )
    cs_gpio = ["PB4", "PB7", "PB10", "PB11"]
    for channel, (ref, gpio) in enumerate(zip(encoders, cs_gpio), 1):
        sym = symbols.get(ir.components[ref].lib_id)
        if sym is None:
            continue
        by_name = {
            p.name.upper().replace("~", "").replace("{", "").replace("}", ""): p.number
            for p in sym.pins
        }
        cs_pin = by_name.get("CS") or by_name.get("CSN")
        clk_pin = by_name.get("CLK") or by_name.get("SCK")
        mosi_pin = by_name.get("MOSI")
        if cs_pin:
            move(ref, cs_pin, f"ENC{channel}_CS")
            move(mcu_ref, mcu_pins[gpio], f"ENC{channel}_CS")
        if clk_pin:
            move(ref, clk_pin, "SPI_SCK")
        if mosi_pin:
            move(ref, mosi_pin, "SPI_MOSI")

    # FDCAN2 AF9 avoids the HRTIM output bank.
    move(mcu_ref, mcu_pins["PB5"], "CAN_RX")
    move(mcu_ref, mcu_pins["PB6"], "CAN_TX")
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_stm32g4_power_network(
    ir: CircuitIR, symbols: dict[str, SymbolDef], logic_rail: str = "+3V3"
) -> list[str]:
    """Build the datasheet-backed STM32G4 supply decoupling network.

    AN5093 recommends 100 nF per VDD/VSS pair plus about 10 uF for the
    device, and 100 nF + 1 uF on VDDA/VSSA.  The analog domain is fed
    through a ferrite bead.  Reset/BOOT are deliberately not synthesized:
    the current KiCad STM32G474RETx symbol exposes neither pin, so inventing
    pin numbers would be less safe than reporting that catalog defect.
    """
    notes: list[str] = []
    if not any(n.name == logic_rail for n in ir.nets):
        return notes
    numeric_c = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"C(\d+)", r))]
    c_counter = max(numeric_c, default=0) + 1
    numeric_fb = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"FB(\d+)", r))]
    fb_counter = max(numeric_fb, default=0) + 1

    def net_for(ref: str, pin: str) -> str | None:
        return next((n.name for n in ir.nets if (ref, pin) in n.nodes), None)

    def move(ref: str, pin: str, target: str) -> None:
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node != (ref, pin)]
        ir.connect(target, (ref, pin))
        if (ref, pin) in ir.nc_pins:
            ir.nc_pins.remove((ref, pin))

    def cap_count(group: str, rail: str, value: str) -> int:
        count = 0
        for ref, comp in ir.components.items():
            if comp.group != group or comp.lib_id != "Device:C" or comp.value.upper() != value.upper():
                continue
            touched = {n.name for n in ir.nets if any(r == ref for r, _ in n.nodes)}
            if {rail, "GND"} <= touched:
                count += 1
        return count

    def add_cap(group: str, rail: str, value: str) -> None:
        nonlocal c_counter
        ref = f"C{c_counter}"
        c_counter += 1
        ir.add(Component(ref, "Device:C", value, group=group))
        ir.connect(rail, (ref, "1"))
        ir.connect("GND", (ref, "2"))
        notes.append(f"added {ref} {value} STM32 supply decoupling on {rail}")

    for ref, comp in list(ir.components.items()):
        if "STM32G4" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        group = comp.group or "MCU"
        vdd_pins = [p for p in sym.pins if p.name.upper() == "VDD"]
        for p in vdd_pins:
            move(ref, p.number, logic_rail)
        for p in sym.pins:
            if p.name.upper() in {"VSS", "VSSA"}:
                move(ref, p.number, "GND")

        digital_have = cap_count(group, logic_rail, "100nF")
        for _ in range(max(0, len(vdd_pins) - digital_have)):
            add_cap(group, logic_rail, "100nF")
        if cap_count(group, logic_rail, "10uF") == 0:
            add_cap(group, logic_rail, "10uF")

        analog_pins = [p for p in sym.pins if p.name.upper() in {"VDDA", "VREF+"}]
        if analog_pins:
            analog_rail = f"{group}_VDDA"
            for p in analog_pins:
                move(ref, p.number, analog_rail)
            bead_exists = any(
                c.group == group and c.lib_id == "Device:FerriteBead"
                and {logic_rail, analog_rail} <= {
                    n.name for n in ir.nets if any(r == rref for r, _ in n.nodes)
                }
                for rref, c in ir.components.items()
            )
            if not bead_exists:
                fbref = f"FB{fb_counter}"
                fb_counter += 1
                ir.add(Component(fbref, "Device:FerriteBead", "600R@100MHz", group=group))
                ir.connect(logic_rail, (fbref, "1"))
                ir.connect(analog_rail, (fbref, "2"))
                notes.append(f"added {fbref} ferrite isolation for {analog_rail}")
            for value in ("100nF", "1uF"):
                if cap_count(group, analog_rail, value) == 0:
                    add_cap(group, analog_rail, value)
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_stm32g4_system_support(
    ir: CircuitIR, symbols: dict[str, SymbolDef], logic_rail: str = "+3V3"
) -> list[str]:
    """Add reset, boot-mode and standard 10-pin SWD support for STM32G4.

    KiCad names the dual-function system pins by their GPIO aliases (PG10
    and PB8).  ST DS12288/AN5093 identify them as PG10-NRST and PB8-BOOT0;
    PA13/PA14 are SWDIO/SWCLK.  Mapping by pin *name* keeps this valid across
    STM32G4 packages without hard-coding LQFP64 numbers.
    """
    notes: list[str] = []
    counters: dict[str, int] = {}

    def next_ref(prefix: str) -> str:
        if prefix not in counters:
            nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(prefix + r"(\d+)", r))]
            counters[prefix] = max(nums, default=0) + 1
        ref = f"{prefix}{counters[prefix]}"
        counters[prefix] += 1
        return ref

    def move(ref: str, pin: str, target: str) -> None:
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node != (ref, pin)]
        ir.connect(target, (ref, pin))
        ir.nc_pins = [node for node in ir.nc_pins if node != (ref, pin)]

    def touches(ref: str) -> set[str]:
        return {n.name for n in ir.nets if any(r == ref for r, _ in n.nodes)}

    for mcu_ref, comp in list(ir.components.items()):
        if "STM32G4" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        pins = {p.name.upper(): p.number for p in sym.pins}
        required = {"PG10", "PB8", "PA13", "PA14"}
        if not required <= pins.keys():
            notes.append(f"{mcu_ref}: STM32G4 system aliases missing {sorted(required - pins.keys())}")
            continue
        group = comp.group or "MCU"
        move(mcu_ref, pins["PG10"], "NRST")
        move(mcu_ref, pins["PB8"], "BOOT0")
        move(mcu_ref, pins["PA13"], "SWDIO")
        move(mcu_ref, pins["PA14"], "SWCLK")
        if "PB3" in pins:
            move(mcu_ref, pins["PB3"], "SWO")

        # DS12288 Figure 27: 100 nF close to NRST; internal pull-up exists.
        reset_cap = next(
            (r for r, c in ir.components.items() if c.group == group and c.lib_id == "Device:C"
             and c.value.upper() == "100NF" and {"NRST", "GND"} <= touches(r)),
            None,
        )
        if reset_cap is None:
            cref = next_ref("C")
            ir.add(Component(cref, "Device:C", "100nF", group=group))
            ir.connect("NRST", (cref, "1"))
            ir.connect("GND", (cref, "2"))
            notes.append(f"added {cref} 100nF NRST protection capacitor")

        # Default to main flash while retaining a two-pin header to assert
        # BOOT0 high for service/programming.
        boot_pull = next(
            (r for r, c in ir.components.items() if c.group == group and c.lib_id == "Device:R"
             and c.value.upper() == "10K" and {"BOOT0", "GND"} <= touches(r)),
            None,
        )
        if boot_pull is None:
            rref = next_ref("R")
            ir.add(Component(rref, "Device:R", "10k", group=group))
            ir.connect("BOOT0", (rref, "1"))
            ir.connect("GND", (rref, "2"))
            notes.append(f"added {rref} 10k BOOT0 pull-down")

        if not any(c.group == group and c.value == "BOOT_MODE" for c in ir.components.values()):
            jboot = next_ref("J")
            ir.add(Component(jboot, "Connector_Generic:Conn_01x02", "BOOT_MODE", group=group))
            ir.connect(logic_rail, (jboot, "1"))
            ir.connect("BOOT0", (jboot, "2"))
            notes.append(f"added {jboot} BOOT0 service header")

        if not any(c.group == group and c.value == "ARM_SWD_10PIN" for c in ir.components.values()):
            jswd = next_ref("J")
            ir.add(Component(jswd, "Connector_Generic:Conn_02x05_Odd_Even", "ARM_SWD_10PIN", group=group))
            ir.connect(logic_rail, (jswd, "1"))
            ir.connect("SWDIO", (jswd, "2"))
            ir.connect("GND", (jswd, "3"), (jswd, "5"), (jswd, "9"))
            ir.connect("SWCLK", (jswd, "4"))
            if "PB3" in pins:
                ir.connect("SWO", (jswd, "6"))
            else:
                ir.nc_pins.append((jswd, "6"))
            ir.nc_pins.extend([(jswd, "7"), (jswd, "8")])
            ir.connect("NRST", (jswd, "10"))
            notes.append(f"added {jswd} standard 10-pin ARM SWD header")
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes
