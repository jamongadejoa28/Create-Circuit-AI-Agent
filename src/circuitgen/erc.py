"""Self-hosted ERC over the Circuit IR (runs before any file is written).

Check logic ported from SKiDL's default ERC (MIT License, Copyright (c)
Dave Vandenbout, src/skidl/erc.py): unconnected pins, misconnected
no-connect pins, 0/1-pin nets, pairwise pin-type conflicts, and net drive
sufficiency. Operates on the IR + symbol definitions instead of live SKiDL
objects (deliberately avoids SKiDL's class-attribute erc_list and
default_circuit binding pitfalls).
"""

from __future__ import annotations

import re

from .ir import CircuitIR, SymbolDef, ValidationIssue
from .pins import ERROR, OK, PIN_INFO, WARNING, PinDrive, PinType, pin_conflict
from .netnames import GROUND_NAMES

_SEV = {WARNING: "warning", ERROR: "error"}


def check_circuit(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += _check_structure(ir, symbols)
    issues += _check_pins(ir, symbols)
    issues += _check_nets(ir, symbols)
    issues += _check_extended(ir, symbols)
    return issues


# Ground-ish net names for the extended rules; a net is also treated as
# ground/power if a power symbol of that kind is a member.


def _power_symbol_values(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> set[str]:
    """Values of supply power symbols (power_in pin, e.g. +5V/GND) on a net."""
    vals = set()
    for ref, pin_no in net.nodes:
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        if sym is None or not sym.is_power:
            continue
        try:
            if sym.pin(pin_no).etype == PinType.PWRIN:
                vals.add(comp.value)
        except KeyError:
            pass
    return vals


def net_kind(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> str:
    """'gnd' | 'power' | 'signal' for extended-rule purposes."""
    vals = _power_symbol_values(ir, symbols, net)
    if vals & GROUND_NAMES or net.name in GROUND_NAMES:
        return "gnd"
    if vals or any(
        (t := _pin_type(ir, symbols, r, str(p))) == PinType.PWROUT
        for r, p in net.nodes
    ) or _source_terminals(ir, symbols, net):
        return "power"
    return "signal"


def _source_terminals(ir, symbols, net) -> bool:
    """CANDIDATE FIX (scratch): net holds a terminal of a cell/battery."""
    for ref, _pin in net.nodes:
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        if sym is not None and sym.is_source:
            return True
    return False



def two_pin_bridges(
    ir: CircuitIR, symbols: dict[str, SymbolDef], prefix: str, net_name: str
) -> list[str]:
    """Nets bridged to `net_name` through a prefix part on exactly two nets.

    Unused pads (not on a net) do not count. A four-pin shunt or a
    feedthrough that also sits on SDA is not a 2-terminal pull-up or
    decoupling cap.
    """
    pin_net = {
        (ref, str(pin)): net.name for net in ir.nets for ref, pin in net.nodes
    }
    bridged: list[str] = []
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if (sym is None
                or (sym.reference_prefix or "").upper() != (prefix or "").upper()):
            continue
        connected = {
            pin_net[(ref, str(p.number))]
            for p in sym.pins
            if (ref, str(p.number)) in pin_net
        }
        if len(connected) != 2 or net_name not in connected:
            continue
        bridged.extend(n for n in connected if n != net_name)
    return bridged


def _recorded_af_role(
    ir: CircuitIR, symbols: dict[str, SymbolDef], net, suffixes: tuple[str, ...]
) -> str | None:
    """A member pin the datasheet records as one of `suffixes`."""
    from .pinfunctions import device_for, pin_carries_function_ending

    for ref, pin_no in net.nodes:
        comp = ir.components.get(ref)
        if comp is None or device_for(comp.lib_id) is None:
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        for role in suffixes:
            if pin_carries_function_ending(comp.lib_id, sym, str(pin_no), role):
                return role
    return None


def _net_label_is_bus_evidence(ir: CircuitIR, net) -> bool:
    """The net name may name a bus only when no datasheet-backed pin is on it.

    STM32 PC13 plus anything on a net called SCK is still GPIO unless a
    pin name or recorded AF already said SCK. ESP32 has no table, so the
    name SDA is the evidence. A flash on SCK with the MCU not yet a
    member is still a labelled bus.
    """
    from .pinfunctions import device_for

    return not any(
        (comp := ir.components.get(ref)) is not None
        and device_for(comp.lib_id) is not None
        for ref, _ in net.nodes
    )


def i2c_member_role(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> str | None:
    """SDA or SCL from a member pin name or recorded AF, not from the net label.

    Pull-ups still use `i2c_line_role` (ESP32 IO21 needs the name SDA).
    A capacitor across the bus must not fire on two nets that are only
    called SDA/SCL — that is a 555 timing C with unfortunate labels.
    """
    for ref, pin_no in net.nodes:
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        if sym is None:
            continue
        try:
            name = (sym.pin(str(pin_no)).name or "").upper()
        except KeyError:
            continue
        if name in ("SDA", "SCL"):
            return name
        for role in ("SDA", "SCL"):
            if name.endswith(("/" + role, "_" + role)):
                return role
    return _recorded_af_role(ir, symbols, net, ("SDA", "SCL"))


def i2c_line_role(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> str | None:
    """SDA or SCL, from the same facts `is_i2c_net` already uses.

    Member pin names first (a sensor SDA pin is electrical). Then a
    recorded MCU AF (I2C*_SDA on PA14). A net label is last, and only
    when no datasheet-backed pin is on the net — ESP32 IO21 has no table,
    so the name SDA is the evidence; STM32 PC13 on a net called SDA is
    not a bus just because of the label.
    """
    member = i2c_member_role(ir, symbols, net)
    if member:
        return member
    if not _net_label_is_bus_evidence(ir, net):
        return None
    name = net.name.upper()
    if name in ("SDA", "SCL"):
        return name
    # I2C1_SDA is ST's own HAL naming and MCU_SDA is what block
    # namespacing produces; exact match alone left those buses with no
    # pull-up at all, which is the failure this detection exists to catch.
    for role in ("SDA", "SCL"):
        if name.endswith(("_" + role, "-" + role)):
            return role
    return None


def is_i2c_net(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> bool:
    """One definition of "this net is an I2C bus line".

    Used by the checker that reports a missing pull-up and by the pass that
    adds one, so the two can never disagree about which nets are a bus.
    """
    return i2c_line_role(ir, symbols, net) is not None


def capacitors_across_i2c_lines(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str, str]]:
    """A 2-pin capacitor whose pins sit on two I2C bus lines.

    Same `i2c_member_role` (pin name or recorded AF), not the net-label
    fallback pull-ups use for ESP32. Same prefix-C test as
    `two_pin_bridges` — a resistor across the bus is not a bypass.
    SDA and SCL member nets among the connections, even if a third pad
    sits on a rail. Figure 12 of SBOS231I draws the 0.01 µF supply
    bypass at V+ to GND, not between SDA and SCL (pdf index 18).
    `decoupling-cap-per-ic` is the same placement: VCC to ground.
    """
    pin_net = {
        (ref, str(pin)): net.name for net in ir.nets for ref, pin in net.nodes
    }
    out: list[tuple[str, str, str]] = []
    for ref, comp in ir.components.items():
        if ref.startswith("#"):
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None or (sym.reference_prefix or "").upper() != "C":
            continue
        connected = {
            pin_net[(ref, str(p.number))]
            for p in sym.pins
            if (ref, str(p.number)) in pin_net
        }
        by_role: dict[str, str] = {}
        for name in connected:
            net = next(n for n in ir.nets if n.name == name)
            role = i2c_member_role(ir, symbols, net)
            if role in ("SDA", "SCL") and role not in by_role:
                by_role[role] = name
        if "SDA" in by_role and "SCL" in by_role:
            out.append((ref, by_role["SDA"], by_role["SCL"]))
    return out


def _dominant_pin_net(
    counts: dict[str, int], tiebreak: dict[str, int] | None = None
) -> str | None:
    """The net most of this IC's pins of one kind sit on.

    Pin-list order is not a vote — STM32G474 PWRIN starts at VBAT, which
    is not the VDD rail Figure 12 / §9 mean by supply. A tie is not
    broken by that order either; `tiebreak` is the unambiguous VDD/VCC/V+
    count from `UNAMBIGUOUS_SUPPLY_NAMES` (the same set that refused to
    guess VDDIO vs VDDCORE). Still tied → None, and the capacitor is
    taken off the bus rather than parked on VBAT.
    """
    if not counts:
        return None
    top = max(counts.values())
    names = [n for n, c in counts.items() if c == top]
    if len(names) == 1:
        return names[0]
    if tiebreak:
        best = max((tiebreak.get(n, 0) for n in names), default=0)
        tied = [n for n in names if tiebreak.get(n, 0) == best]
        if len(tied) == 1 and best > 0:
            return tied[0]
    return None


def i2c_device_supply_and_return(
    ir: CircuitIR, symbols: dict[str, SymbolDef], bus_a: str, bus_b: str
) -> tuple[str | None, str | None]:
    supply, gnd, _reason = i2c_device_bypass_rails(ir, symbols, bus_a, bus_b)
    return supply, gnd


def i2c_device_bypass_rails(
    ir: CircuitIR, symbols: dict[str, SymbolDef], bus_a: str, bus_b: str
) -> tuple[str | None, str | None, str]:
    """PWRIN net and ground-pin net of an IC that carries SDA/SCL.

    Figure 12 / §9 put the bypass at the sensor's V+ and GND, not at the
    first net whose kind is gnd (AGND listed before GND) and not at a
    rail *name* the caller passed in (V+ may sit on VCC).
    A pin named SDA/SCL wins over recorded AF, so TMP100 V+/GND is
    preferred to an MCU that is only on the bus by Table 12.
    Among one IC's rails, the net that already holds the most of its
    non-ground PWRIN pins wins — not the first PWRIN in the symbol.
    Two named devices that do not share a (supply, return) pair: None.
    Net node order is not a vote.
    """
    from collections import Counter

    from .netnames import UNAMBIGUOUS_SUPPLY_NAMES, is_ground_pin

    pin_net = {
        (ref, str(pin)): net.name for net in ir.nets for ref, pin in net.nodes
    }
    named: list[tuple[str, str]] = []
    af_only: list[tuple[str, str]] = []
    seen: set[str] = set()
    bus = {bus_a, bus_b}
    for net in ir.nets:
        if net.name not in bus or i2c_member_role(ir, symbols, net) is None:
            continue
        for ref, pin_no in net.nodes:
            if ref in seen:
                continue
            comp = ir.components.get(ref)
            if comp is None:
                continue
            sym = symbols.get(comp.lib_id)
            if (
                sym is None
                or sym.is_power
                or (sym.reference_prefix or "").upper() == "C"
                or not _pin_carries_i2c_name_or_af(comp.lib_id, sym, str(pin_no))
            ):
                continue
            seen.add(ref)
            supply_n: Counter[str] = Counter()
            gnd_n: Counter[str] = Counter()
            unambiguous: Counter[str] = Counter()
            named_bus = False
            for p in sym.pins:
                pname = (p.name or "").upper()
                if pname in ("SDA", "SCL") or pname.endswith(
                    ("/SDA", "_SDA", "/SCL", "_SCL")
                ):
                    named_bus = True
                n = pin_net.get((ref, p.number))
                if not n:
                    continue
                if is_ground_pin(p.name or ""):
                    gnd_n[n] += 1
                elif p.etype == PinType.PWRIN:
                    supply_n[n] += 1
                    if pname in UNAMBIGUOUS_SUPPLY_NAMES:
                        unambiguous[n] += 1
            supply = _dominant_pin_net(supply_n, unambiguous)
            gnd = _dominant_pin_net(gnd_n)
            if supply and gnd and supply != gnd:
                (named if named_bus else af_only).append((supply, gnd))
    if named:
        one = _one_rail_pair(named)
        if one[0] and one[1]:
            return one[0], one[1], "ok"
        return None, None, "disagree"
    if af_only:
        one = _one_rail_pair(af_only)
        if one[0] and one[1]:
            return one[0], one[1], "ok"
        return None, None, "disagree"
    return None, None, "missing"



def _one_rail_pair(pairs: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """The single (supply, gnd) those ICs agree on, or nothing.

    `named[0]` followed the bus net's node list, so two TMP100s on +3V3
    and +5V moved C1 with whichever sensor was listed first.
    """
    uniq = set(pairs)
    if len(uniq) == 1:
        return next(iter(uniq))
    return None, None


def _pin_carries_i2c_name_or_af(lib_id: str, sym: SymbolDef, pin: str) -> bool:
    from .pinfunctions import pin_carries_function_ending

    try:
        name = (sym.pin(str(pin)).name or "").upper()
    except KeyError:
        return False
    if name in ("SDA", "SCL") or name.endswith(("/SDA", "_SDA", "/SCL", "_SCL")):
        return True
    return pin_carries_function_ending(lib_id, sym, pin, "SDA") or (
        pin_carries_function_ending(lib_id, sym, pin, "SCL")
    )


_SPI_TOKEN_TO_AF = {
    # DS12288 Table 12: SPI*_SCK / MOSI / MISO / NSS. CLK/DI/DO/CS are
    # vendor pin names (W25Q) and also 4017 clocks / AD8231 chip-selects.
    "SCK": "SCK",
    "MOSI": "MOSI",
    "MISO": "MISO",
    "NSS": "NSS",
}


def _spi_name_tokens(text: str) -> list[str]:
    raw = (text or "").replace("~", "").replace("{", "").replace("}", "")
    return [
        re.sub(r"[^A-Z0-9]", "", part.upper())
        for part in re.split(r"[/_-]", raw)
        if part.strip()
    ]


def spi_line_role(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> str | None:
    """SCK, MOSI, MISO, or NSS.

    Member pin names first when they are Table 12 suffixes (SCK/MOSI/
    MISO/NSS). Then a recorded MCU AF (SPI*_SCK on PA5). A net label is
    last, and only when no datasheet-backed pin is on the net. STM32
    GPIO on a net called SCK is not a SPI clock. A 4017 CLK pin is not
    SCK. Chip-select may be any GPIO; SCK/MOSI/MISO must be recorded AF
    pins once the net is a bus.
    """
    for ref, pin_no in net.nodes:
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        if sym is None:
            continue
        try:
            name = sym.pin(str(pin_no)).name or ""
        except KeyError:
            continue
        for token in _spi_name_tokens(name):
            role = _SPI_TOKEN_TO_AF.get(token)
            if role:
                return role
    af = _recorded_af_role(ir, symbols, net, ("SCK", "MOSI", "MISO", "NSS"))
    if af:
        return af
    if not _net_label_is_bus_evidence(ir, net):
        return None
    for token in _spi_name_tokens(net.name):
        role = _SPI_TOKEN_TO_AF.get(token)
        if role:
            return role
    return None


def is_spi_net(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> bool:
    """One definition of a 4-wire SPI bus line. Join and checker share it."""
    return spi_line_role(ir, symbols, net) is not None


def pin_name_is_chip_select(pin_name: str) -> bool:
    """Whether a symbol pin is named /CS (~{CS}).

    Not folded into `_SPI_TOKEN_TO_AF`: AD8231 and other parts also use
    a CS token. Serial-flash /CS pull-up uses lib_id plus this name.
    """
    return "CS" in _spi_name_tokens(pin_name)


def pin_name_is_write_protect(pin_name: str) -> bool:
    """Whether a Memory_Flash pin is /WP (~{WP})."""
    return "WP" in _spi_name_tokens(pin_name)


def pin_name_is_hold(pin_name: str) -> bool:
    """Whether a Memory_Flash pin is /HOLD (~{HOLD})."""
    return "HOLD" in _spi_name_tokens(pin_name)


def flash_cs_connections(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str, str]]:
    """Each Memory_Flash /CS pin: (flash_ref, pin_no, net_name).

    MCU NSS GPIO or net labels alone do not count — only the flash symbol's
    /CS pin on a Memory_Flash: lib_id.
    """
    pin_net = {
        (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
    }
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref, comp in ir.components.items():
        if not comp.lib_id.startswith("Memory_Flash:"):
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        for p in sym.pins:
            if not pin_name_is_chip_select(p.name or ""):
                continue
            pin = str(p.number)
            net = pin_net.get((ref, pin))
            if net and (ref, pin) not in seen:
                seen.add((ref, pin))
                out.append((ref, pin, net))
    return out


def flash_wp_hold_connections(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str, str, str]]:
    """Each Memory_Flash /WP or /HOLD pin: (flash_ref, pin_no, net_name, kind).

    kind is ``WP`` or ``HOLD``. Same lib_id gate as ``flash_cs_connections``.
    """
    pin_net = {
        (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
    }
    out: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref, comp in ir.components.items():
        if not comp.lib_id.startswith("Memory_Flash:"):
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        for p in sym.pins:
            name = p.name or ""
            if pin_name_is_write_protect(name):
                kind = "WP"
            elif pin_name_is_hold(name):
                kind = "HOLD"
            else:
                continue
            pin = str(p.number)
            net = pin_net.get((ref, pin))
            if net and (ref, pin) not in seen:
                seen.add((ref, pin))
                out.append((ref, pin, net, kind))
    return out


def flash_supply_net(
    ir: CircuitIR, symbols: dict[str, SymbolDef], flash_ref: str
) -> str | None:
    """The board supply net the flash VCC (PWRIN) pin sits on."""
    pin_net = {
        (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
    }
    comp = ir.components.get(flash_ref)
    sym = symbols.get(comp.lib_id) if comp else None
    if sym is None:
        return None
    for p in sym.pins:
        if p.etype != PinType.PWRIN:
            continue
        name = (p.name or "").upper()
        from .netnames import is_ground_pin
        if is_ground_pin(name):
            continue
        net_name = pin_net.get((flash_ref, str(p.number)))
        if not net_name:
            continue
        net = next((n for n in ir.nets if n.name == net_name), None)
        if net and net_kind(ir, symbols, net) == "power":
            return net_name
    return None


def flash_return_net(
    ir: CircuitIR, symbols: dict[str, SymbolDef], flash_ref: str
) -> str | None:
    """The net the flash VSS/GND pin sits on — name-independent."""
    from .netnames import is_ground_pin

    pin_net = {
        (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
    }
    comp = ir.components.get(flash_ref)
    sym = symbols.get(comp.lib_id) if comp else None
    if sym is None:
        return None
    for p in sym.pins:
        if not is_ground_pin(p.name or ""):
            continue
        net_name = pin_net.get((flash_ref, str(p.number)))
        if net_name:
            return net_name
    return None


def flash_cs_on_return(
    ir: CircuitIR, symbols: dict[str, SymbolDef], flash_ref: str, cs_net: str
) -> bool:
    """True when /CS is asserted low against the flash return.

    Same net as the flash VSS pin, or a net `net_kind` classifies as
    ground. An R from that net to VCC is a rail load, not a CS pull-up.
    """
    if not cs_net:
        return False
    ret = flash_return_net(ir, symbols, flash_ref)
    if ret and cs_net == ret:
        return True
    net = next((n for n in ir.nets if n.name == cs_net), None)
    return net is not None and net_kind(ir, symbols, net) == "gnd"


def flash_cs_bus_net(
    ir: CircuitIR, symbols: dict[str, SymbolDef], flash_ref: str
) -> str | None:
    """A net that can drive /CS, other than the flash rails.

    Prefer a net that already has an R to the flash VCC (a pull-up already
    placed). Else a net whose members are SPI NSS/CS by `spi_line_role`.
    """
    supply = flash_supply_net(ir, symbols, flash_ref)
    ret = flash_return_net(ir, symbols, flash_ref)
    skip = {n for n in (supply, ret) if n}
    for net in ir.nets:
        if net.name in skip:
            continue
        if supply and supply in two_pin_bridges(ir, symbols, "R", net.name):
            return net.name
    for net in ir.nets:
        if net.name in skip:
            continue
        if spi_line_role(ir, symbols, net) == "NSS":
            return net.name
        if pin_name_is_chip_select(net.name):
            return net.name
    return None


def spi_flash_cs_tracks_vcc(
    ir: CircuitIR, symbols: dict[str, SymbolDef], flash_ref: str, cs_net: str
) -> bool:
    """Whether /CS already tracks that flash's VCC (§4.1, pdf index 9).

    Direct tie: /CS net is the same net as the flash VCC pin. Otherwise an
    R must bridge /CS to that same VCC net — not an arbitrary supply rail.
    The flash return (VSS net or a ground net) is never tracking VCC:
    /CS is active-low, so that net means selected. An R from there to VCC
    is a rail load.
    """
    if flash_cs_on_return(ir, symbols, flash_ref, cs_net):
        return False
    supply = flash_supply_net(ir, symbols, flash_ref)
    if not supply:
        return False
    if cs_net == supply:
        return True
    return supply in two_pin_bridges(ir, symbols, "R", cs_net)


def shorted_bypass_capacitors(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str]]:
    """Each 2-pin C with both pins on one net: (ref, that net).

    Shared by the normalize repair pass and tests. A bypass cap must bridge
    two potentials (knowledge: decoupling-cap-per-ic).
    """
    pin_net = {
        (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
    }
    out: list[tuple[str, str]] = []
    for ref, comp in ir.components.items():
        if comp.lib_id != "Device:C":
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None or len(sym.pins) != 2:
            continue
        nets = {
            pin_net.get((ref, str(p.number)))
            for p in sym.pins
            if (ref, str(p.number)) not in ir.nc_pins
        } - {None}
        if len(nets) == 1:
            out.append((ref, next(iter(nets))))
    return out


def spi_hub_af_failures(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str]]:
    """Hub membership on SCK/MOSI/MISO that is not a recorded SPI function.

    Same facts `join_hub_to_i2c_buses` uses for those roles: `spi_line_role`,
    `hub_ref`, `pin_carries_function_ending`. NSS/CS is not in this list —
    a GPIO chip-select is valid. No table means no verdict.
    """
    from .normalize import hub_ref
    from .pinfunctions import device_for, pin_carries_function_ending

    hub = hub_ref(ir, symbols)
    if hub is None:
        return []
    lib_id = ir.components[hub].lib_id
    device = device_for(lib_id)
    if device is None:
        return []
    sym = symbols.get(lib_id)
    if sym is None:
        return []
    source = device.get("source") or {}
    cited = (
        f"{source.get('document', 'datasheet')}, "
        f"{source.get('table', 'pin table')}"
    )
    out: list[tuple[str, str]] = []
    for net in ir.nets:
        role = spi_line_role(ir, symbols, net)
        if role not in ("SCK", "MOSI", "MISO"):
            continue
        on_hub = [p for r, p in net.nodes if r == hub]
        if not on_hub:
            out.append((
                f"{hub}:{net.name}",
                f"{hub} is not on SPI {role} net {net.name}",
            ))
            continue
        if any(pin_carries_function_ending(lib_id, sym, p, role) for p in on_hub):
            continue
        pin = on_hub[0]
        out.append((
            f"{hub}.{pin}",
            f"{hub}.{pin} is on SPI {role} net {net.name} but is not a "
            f"recorded {role} pin ({cited})",
        ))
    return out


def i2c_hub_af_failures(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[tuple[str, str]]:
    """Hub membership on an I2C net that is not a recorded SDA/SCL function.

    Same four facts `join_hub_to_i2c_buses` uses: `is_i2c_net`,
    `i2c_line_role`, `hub_ref`, `pin_carries_function_ending`. No recorded
    table means no verdict — the fixer also refuses to invent a GPIO.
    """
    from .normalize import hub_ref
    from .pinfunctions import device_for, pin_carries_function_ending

    hub = hub_ref(ir, symbols)
    if hub is None:
        return []
    lib_id = ir.components[hub].lib_id
    device = device_for(lib_id)
    if device is None:
        return []
    sym = symbols.get(lib_id)
    if sym is None:
        return []
    source = device.get("source") or {}
    cited = (
        f"{source.get('document', 'datasheet')}, "
        f"{source.get('table', 'pin table')}"
    )
    out: list[tuple[str, str]] = []
    for net in ir.nets:
        if not is_i2c_net(ir, symbols, net):
            continue
        role = i2c_line_role(ir, symbols, net)
        if role is None:
            continue
        on_hub = [p for r, p in net.nodes if r == hub]
        if not on_hub:
            out.append((
                f"{hub}:{net.name}",
                f"{hub} is not on I2C {role} net {net.name}",
            ))
            continue
        if any(pin_carries_function_ending(lib_id, sym, p, role) for p in on_hub):
            continue
        pin = on_hub[0]
        out.append((
            f"{hub}.{pin}",
            f"{hub}.{pin} is on I2C {role} net {net.name} but is not a "
            f"recorded {role} pin ({cited})",
        ))
    return out


def _check_extended(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    """Plan §8.2 extended rules (MCU/IC-grade lint).

    Values referenced by these rules are grounded in the curated knowledge
    base (decoupling-cap-per-ic, pullup-resistor-sizing, ...), not invented
    here. Severities: electrical impossibilities are errors; best-practice
    omissions are warnings the agent repair loop can act on.
    """
    issues: list[ValidationIssue] = []
    net_kinds = {net.name: net_kind(ir, symbols, net) for net in ir.nets}
    nets_by_name = {net.name: net for net in ir.nets}

    # -- shorted power rails: two different supply symbols on one net --
    for net in ir.nets:
        vals = _power_symbol_values(ir, symbols, net)
        if len(vals) > 1:
            issues.append(
                _issue("power_rails_shorted", "error", f"net:{net.name}", f"net {net.name} ties different power rails together: {sorted(vals)}")
            )

    # nets each component pin belongs to: ref -> {pin_no: net}
    pin_net: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for ref, pin_no in net.nodes:
            pin_net[(ref, str(pin_no))] = net.name

    def _two_pin_bridges(prefix: str, net_name: str) -> list[str]:
        return two_pin_bridges(ir, symbols, prefix, net_name)

    # -- decoupling per IC power pin (knowledge: decoupling-cap-per-ic) --
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        pwr_nets = {
            pin_net.get((ref, p.number))
            for p in sym.pins
            if p.etype == PinType.PWRIN
        } - {None}
        for net_name in sorted(pwr_nets):
            if net_kinds.get(net_name) != "power":
                continue
            has_cap = any(
                net_kinds.get(other) == "gnd"
                for other in _two_pin_bridges("C", net_name)
            )
            if not has_cap:
                issues.append(
                    _issue("decoupling_missing", "warning", f"{ref}@{net_name}", f"{ref} power net {net_name} has no decoupling capacitor to ground (0.01-0.1uF per IC — knowledge: decoupling-cap-per-ic)")
                )

    # -- I2C pull-ups (knowledge: pullup-resistor-sizing) --
    for net in ir.nets:
        if not is_i2c_net(ir, symbols, net):
            continue
        has_pullup = any(
            net_kinds.get(other) == "power"
            for other in _two_pin_bridges("R", net.name)
        )
        if not has_pullup:
            issues.append(
                _issue("i2c_pullup_missing", "warning", f"net:{net.name}", f"I2C net {net.name} (SDA/SCL pins) has no pull-up resistor to a power rail (typ. 10k — knowledge: pullup-resistor-sizing)")
            )

    seen_cs: set[tuple[str, str]] = set()
    for flash_ref, _pin, cs_net in flash_cs_connections(ir, symbols):
        key = (flash_ref, cs_net)
        if key in seen_cs:
            continue
        seen_cs.add(key)
        if spi_flash_cs_tracks_vcc(ir, symbols, flash_ref, cs_net):
            continue
        supply = flash_supply_net(ir, symbols, flash_ref) or "?"
        issues.append(
            _issue(
                "spi_flash_cs_pullup_missing",
                "warning",
                f"net:{cs_net}",
                f"flash /CS net {cs_net} does not track the device VCC net "
                f"{supply} at power-up (knowledge: w25q32jv-cs-tracks-vcc; "
                f"pdf index 9)",
            )
        )

    for ref, a, b in capacitors_across_i2c_lines(ir, symbols):
        issues.append(
            _issue(
                "capacitor_across_i2c",
                "warning",
                ref,
                f"{ref} bridges I2C lines {a} and {b} — supply bypass is "
                f"VCC to ground (knowledge: decoupling-cap-per-ic; "
                f"SBOS231I Figure 12, pdf index 18)",
            )
        )

    # -- design sanity for 2-pin parts (caught live: a 7B model produced a
    #    switch straight across the rails and an LED with one pin NC'd —
    #    ERC-legal, functionally nonsense) --
    nc = {(r, str(p)) for r, p in ir.nc_pins}
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#") or len(sym.pins) != 2:
            continue
        kinds = []
        for p in sym.pins:
            net_name = pin_net.get((ref, p.number))
            kinds.append(net_kinds.get(net_name) if net_name else None)
        if sym.reference_prefix == "SW" and sorted(k for k in kinds if k) == ["gnd", "power"]:
            issues.append(
                _issue("switch_across_rails", "error", ref, f"{ref} connects a power rail directly to ground — closing it shorts the supply; put it in series with the load instead")
            )
        is_tvs = "TVS" in comp.lib_id.upper() or "TVS" in comp.value.upper()
        if sym.reference_prefix == "D" and not is_tvs and sorted(k for k in kinds if k) == ["gnd", "power"]:
            # knowledge: led-series-resistor — a diode/LED straight across
            # the rails has no current limiting and will be destroyed
            issues.append(
                _issue("diode_across_rails", "error", ref, f"{ref} sits directly between a power rail and ground with no current limiting — insert a series resistor (R = (Vsupply - Vf) / If)")
            )
        if any((ref, p.number) in nc for p in sym.pins):
            issues.append(
                _issue("dead_two_pin_component", "warning", ref, f"{ref} has a no-connect pin — a 2-pin component with an open pin does nothing; wire both pins or remove it")
            )

    # -- footprint presence (real parts only) --
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        if not comp.footprint:
            issues.append(
                _issue("footprint_missing", "warning", ref, f"{ref} has no footprint assigned")
            )

    return issues


def _issue(rule: str, severity: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue("circuitgen-erc", rule, severity, path, message)


_FORBIDDEN_NAME_CHARS = set('/\\"\n\r')


def _bad_name(s: str) -> bool:
    return not s or any(ch in _FORBIDDEN_NAME_CHARS for ch in s)


def _check_structure(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    """IR-level sanity: every referenced component/pin/symbol must exist."""
    issues = []
    if _bad_name(ir.name) or any(ch in ir.name for ch in " :*?<>|"):
        issues.append(
            _issue("invalid_name", "error", "circuit", f"circuit name {ir.name!r} is not filename-safe")
        )
    for net in ir.nets:
        if _bad_name(net.name):
            issues.append(
                _issue("invalid_name", "error", f"net:{net.name!r}", f"net name {net.name!r} is empty or contains /, \\, quote, or newline")
            )
    for ref, comp in ir.components.items():
        if _bad_name(ref):
            issues.append(
                _issue("invalid_name", "error", repr(ref), f"reference {ref!r} is empty or contains /, \\, quote, or newline")
            )
        if comp.binding_error:
            issues.append(_issue(
                "component_binding_conflict", "error", ref,
                f"{ref}: {comp.binding_error}",
            ))
        sym = symbols.get(comp.lib_id)
        if sym is not None:
            units = {p.unit for p in sym.pins}
            if 0 in units and len(units - {0}) > 1:
                # A unit-0 pin on a multi-unit symbol is drawn on EVERY
                # placed instance; the stub+label emitter cannot represent
                # that (mostly IEEE-variant libraries). Use the standard
                # library's per-unit power-unit form instead.
                issues.append(
                    _issue("unit0_pins_unsupported", "error", ref, f"{ref} ({comp.lib_id}) mixes unit-0 pins with multiple placed units — unsupported")
                )
        if comp.lib_id not in symbols:
            issues.append(
                _issue("unknown_symbol", "error", ref, f"{ref}: symbol {comp.lib_id} not in library set")
            )
    seen: set[tuple[str, str]] = set()
    for net in ir.nets:
        for ref, pin_no in net.nodes:
            if ref not in ir.components:
                issues.append(
                    _issue("unknown_component", "error", f"net:{net.name}", f"net {net.name} references unknown component {ref}")
                )
                continue
            comp = ir.components[ref]
            sym = symbols.get(comp.lib_id)
            if sym is not None and not sym.has_pin(pin_no):
                issues.append(
                    _issue("unknown_pin", "error", f"{ref}.{pin_no}", f"{ref} ({comp.lib_id}) has no pin {pin_no}")
                )
            if (ref, str(pin_no)) in seen:
                issues.append(
                    _issue("pin_multiple_nets", "error", f"{ref}.{pin_no}", f"pin {ref}.{pin_no} appears in more than one net")
                )
            seen.add((ref, str(pin_no)))
    return issues


def _pin_type(ir: CircuitIR, symbols: dict[str, SymbolDef], ref: str, pin_no: str) -> PinType | None:
    comp = ir.components.get(ref)
    if comp is None:
        return None
    sym = symbols.get(comp.lib_id)
    if sym is None:
        return None
    try:
        return sym.pin(pin_no).etype
    except KeyError:
        return None


def _check_pins(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[ValidationIssue]:
    """SKiDL dflt_part_erc: unconnected pins / misconnected NC pins."""
    issues = []
    connected = {(ref, str(p)) for net in ir.nets for ref, p in net.nodes}
    nc = {(ref, str(p)) for ref, p in ir.nc_pins}
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue  # already reported by _check_structure
        for pin in sym.pins:
            key = (ref, pin.number)
            if key in connected:
                if pin.etype == PinType.NOCONNECT:
                    issues.append(
                        _issue("nc_pin_connected", "error", f"{ref}.{pin.number}", f"no-connect pin {ref}.{pin.number} is wired to a net")
                    )
                if key in nc:
                    issues.append(
                        _issue("nc_marked_but_connected", "error", f"{ref}.{pin.number}", f"pin {ref}.{pin.number} marked NC but wired to a net")
                    )
            else:
                if pin.etype != PinType.NOCONNECT and key not in nc:
                    # Error, not warning: KiCad's pin_not_connected defaults
                    # to error for every pin type, and agreeing with the
                    # oracle fails the pipeline before a doomed emission.
                    issues.append(
                        _issue("unconnected_pin", "error", f"{ref}.{pin.number}", f"unconnected pin {ref}.{pin.number} ({sym.lib_id} {pin.name}) — connect it or mark it NC")
                    )
    return issues


def _check_nets(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[ValidationIssue]:
    """SKiDL dflt_net_erc: pin count, pairwise conflicts, drive sufficiency."""
    issues = []
    for net in ir.nets:
        path = f"net:{net.name}"
        if len(net.nodes) == 0:
            issues.append(_issue("empty_net", "warning", path, f"no pins attached to net {net.name}"))
            continue
        if len(net.nodes) == 1:
            # Error, not warning: the stub+label emitter turns every 1-pin
            # net into a KiCad isolated_pin_label violation, so this can
            # never pass the oracle. Connect a second pin or mark it NC.
            ref, pin_no = net.nodes[0]
            issues.append(
                _issue("single_pin_net", "error", path, f"net {net.name} has only one pin ({ref}.{pin_no}) — connect another pin to it or remove the net and mark {ref}.{pin_no} NC")
            )

        typed = [
            (ref, str(p), t)
            for ref, p in net.nodes
            if (t := _pin_type(ir, symbols, ref, str(p))) is not None
        ]

        for i in range(len(typed)):
            for j in range(i + 1, len(typed)):
                ri, pi, ti = typed[i]
                rj, pj, tj = typed[j]
                sev, msg = pin_conflict(ti, tj)
                if sev != OK:
                    detail = f" ({msg})" if msg else ""
                    issues.append(
                        _issue("pin_conflict", _SEV[sev], path, f"net {net.name}: {ri}.{pi} ({ti.name}) × {rj}.{pj} ({tj.name}){detail}")
                    )

        if typed:
            net_drive = max(PIN_INFO[t]["drive"] for _, _, t in typed)
            if net_drive <= PinDrive.NONE:
                issues.append(_issue("no_driver", "warning", path, f"no drivers for net {net.name}"))
            for ref, pin_no, t in typed:
                if PIN_INFO[t]["min_rcv"] > net_drive:
                    issues.append(
                        _issue("insufficient_drive", "warning", path, f"insufficient drive on net {net.name} for pin {ref}.{pin_no} ({t.name})")
                    )
    return issues
