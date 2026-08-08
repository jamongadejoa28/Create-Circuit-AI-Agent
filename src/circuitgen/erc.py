"""Self-hosted ERC over the Circuit IR (runs before any file is written).

Check logic ported from SKiDL's default ERC (MIT License, Copyright (c)
Dave Vandenbout, src/skidl/erc.py): unconnected pins, misconnected
no-connect pins, 0/1-pin nets, pairwise pin-type conflicts, and net drive
sufficiency. Operates on the IR + symbol definitions instead of live SKiDL
objects (deliberately avoids SKiDL's class-attribute erc_list and
default_circuit binding pitfalls).
"""

from __future__ import annotations

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


def _net_kind(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> str:
    """'gnd' | 'power' | 'signal' for extended-rule purposes."""
    vals = _power_symbol_values(ir, symbols, net)
    if vals & GROUND_NAMES or net.name in GROUND_NAMES:
        return "gnd"
    if vals or any(
        (t := _pin_type(ir, symbols, r, str(p))) == PinType.PWROUT
        for r, p in net.nodes
    ):
        return "power"
    return "signal"


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
    net_kinds = {net.name: _net_kind(ir, symbols, net) for net in ir.nets}
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
        """Nets bridged to net_name through a 2-pin part with ref prefix."""
        bridged = []
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None or sym.reference_prefix != prefix or len(sym.pins) != 2:
                continue
            nets = {pin_net.get((ref, p.number)) for p in sym.pins}
            if net_name in nets:
                bridged.extend(n for n in nets if n and n != net_name)
        return bridged

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
        pin_names = {
            (symbols[ir.components[r].lib_id].pin(str(p)).name or "").upper()
            for r, p in net.nodes
            if r in ir.components and ir.components[r].lib_id in symbols
            and _pin_type(ir, symbols, r, str(p)) is not None
        }
        # Dedicated I2C pins carry SDA/SCL names (sensors, EEPROMs); MCU GPIO
        # pins usually don't (ESP32: IO21/IO22), so a net NAMED SDA/SCL is
        # treated as equally strong intent.
        named_i2c = net.name.upper() in ("SDA", "SCL")
        if not named_i2c and not any(
            n in ("SDA", "SCL") or n.endswith("/SDA") or n.endswith("/SCL") for n in pin_names
        ):
            continue
        has_pullup = any(
            net_kinds.get(other) == "power"
            for other in _two_pin_bridges("R", net.name)
        )
        if not has_pullup:
            issues.append(
                _issue("i2c_pullup_missing", "warning", f"net:{net.name}", f"I2C net {net.name} (SDA/SCL pins) has no pull-up resistor to a power rail (typ. 10k — knowledge: pullup-resistor-sizing)")
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
        sym = symbols.get(comp.lib_id)
        if sym is not None:
            units = {p.unit for p in sym.pins}
            if 0 in units and (units - {0}):
                # A unit-0 pin on a multi-unit symbol is drawn on EVERY
                # placed instance; the stub+label emitter cannot represent
                # that (mostly IEEE-variant libraries). Use the standard
                # library's per-unit power-unit form instead.
                issues.append(
                    _issue("unit0_pins_unsupported", "error", ref, f"{ref} ({comp.lib_id}) mixes unit-0 pins with multiple units — unsupported")
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
            if sym is not None:
                try:
                    sym.pin(pin_no)
                except KeyError:
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
