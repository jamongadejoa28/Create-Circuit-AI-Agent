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

_SEV = {WARNING: "warning", ERROR: "error"}


def check_circuit(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += _check_structure(ir, symbols)
    issues += _check_pins(ir, symbols)
    issues += _check_nets(ir, symbols)
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
                    issues.append(
                        _issue("unconnected_pin", "warning", f"{ref}.{pin.number}", f"unconnected pin {ref}.{pin.number} ({sym.lib_id} {pin.name})")
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
            ref, pin_no = net.nodes[0]
            issues.append(
                _issue("single_pin_net", "warning", path, f"only one pin ({ref}.{pin_no}) attached to net {net.name}")
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
