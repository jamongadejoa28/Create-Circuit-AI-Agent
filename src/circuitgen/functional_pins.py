"""Functional pin completeness — IR must not reach emission with missing buses.

Three connectivity layers (see docs/STATUS.md):

  A. IR unbound     — pin not in any net (this module)
  B. Geometry       — wire endpoint != pin coordinate (geometry.py)
  C. Label fallback — stub+label, electrically connected (emit.route_metrics)

Emission must not treat C as success while A remains. Router improvements
cannot fix A.
"""

from __future__ import annotations

from .erc import (
    cited_w25q_flash,
    i2c_line_role,
    is_i2c_net,
    is_spi_net,
    pin_name_i2c_role,
    pin_name_is_spi_select,
    pin_name_serial_role,
    pin_name_spi_role,
    spi_line_role,
)
from .ir import CircuitIR, SymbolDef, ValidationIssue
from .pins import PinType


def _issue(rule: str, severity: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue("circuitgen-erc", rule, severity, path, message)


def _pin_net_map(ir: CircuitIR) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for ref, pin in net.nodes:
            out[(ref, str(pin))] = net.name
    return out


def _config_pin_may_float(comp, pin_name: str, node: tuple[str, str], ir: CircuitIR) -> bool:
    """Address / strap pins that datasheets allow to float when marked NC.

    SBOS231I Table 2 (ADD0/ADD1: GND, V+, or float). W25Q WP/HOLD when NC
    (knowledge w25q32jv-wp-hold-active-low — released high, not asserted).
    """
    if node not in ir.nc_pins:
        return False
    name = (pin_name or "").upper().replace("~", "").replace("{", "").replace("}", "")
    if name in {"ADD0", "ADD1", "ADD2"} or name.startswith("ADD"):
        return True
    if cited_w25q_flash(comp):
        from .erc import pin_name_is_hold, pin_name_is_write_protect

        if pin_name_is_write_protect(pin_name) or pin_name_is_hold(pin_name):
            return True
    return False


def _protocols_by_net(ir: CircuitIR) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for contract in ir.interface_contracts:
        out.setdefault(contract.net, set()).add(contract.protocol.lower())
    return out


def _serial_protocol(comp, protocols: set[str]) -> str:
    """Protocol context for a TX/RX-shaped peripheral pin.

    TXD/RXD alone expresses direction, not UART: CAN transceivers use those
    exact names.  Planner contracts are authoritative; the KiCad interface
    library family is a grounded fallback for flat/legacy IR.
    """
    if "uart" in protocols:
        return "UART"
    if "can" in protocols:
        return "CAN"
    nickname = (getattr(comp, "lib_id", "") or "").split(":", 1)[0].upper()
    if "UART" in nickname:
        return "UART"
    if "CAN" in nickname or "LIN" in nickname:
        return "CAN"
    return "SERIAL"


def _uart_controller_af_issues(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    net,
    peripheral_role: str,
) -> list[ValidationIssue]:
    """Verify that a datasheet-backed controller pin carries UART TX/RX."""
    from .pinfunctions import device_for, pin_carries_function_ending

    expected = "RX" if peripheral_role == "TX" else "TX"
    issues: list[ValidationIssue] = []
    for ref, pin_no in net.nodes:
        if ref not in ir.controller_refs:
            continue
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        device = device_for(comp.lib_id) if comp else None
        if sym is None or device is None:  # no recorded table means no verdict
            continue
        if pin_carries_function_ending(comp.lib_id, sym, str(pin_no), expected):
            continue
        source = device.get("source") or {}
        cited = f"{source.get('document', 'datasheet')}, {source.get('table', 'pin table')}"
        issues.append(_issue(
            "functional_controller_af_mismatch",
            "error",
            f"{ref}.{pin_no}",
            f"{ref}.{pin_no} is on UART net {net.name} but is not a recorded "
            f"UART {expected} pin ({cited})",
        ))
    return issues


def check_functional_pin_completeness(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    """Every peripheral interface pin is connected or explicitly allowed.

    Uses the same pin-name facts as ``i2c_member_role`` and ``spi_line_role``,
    not a per-part denylist. A TMP100 SDA pin and a Si7051 SDA pin are the
    same check. The controller may explicitly NC an unused interface; a
    peripheral may not use NC to appear functionally complete.
    """
    issues: list[ValidationIssue] = []
    pin_net = _pin_net_map(ir)
    nc = {(r, str(p)) for r, p in ir.nc_pins}
    controllers = {
        ref for ref in ir.controller_refs
        if ref in ir.components and ir.components[ref].lib_id in symbols
    }
    protocols = _protocols_by_net(ir)

    # A component-level SPI activation is what makes CS/NSS functional.  The
    # token by itself is ambiguous (for example AD8231 also has CS), while a
    # sibling SCK/MOSI/MISO pin on a real net is direct interface evidence.
    active_spi: set[str] = set()
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or ref in controllers:
            continue
        if any(
            pin_name_spi_role(comp, pin.name or "")
            and (ref, str(pin.number)) in pin_net
            for pin in sym.pins
            if not pin.hidden
        ):
            active_spi.add(ref)

    has_functional_peripheral = False

    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        for pin in sym.pins:
            if pin.hidden:
                continue
            node = (ref, pin.number)
            on_net = pin_net.get(node)
            name = pin.name or ""

            if pin.etype == PinType.NOCONNECT:
                continue  # library-declared NC — mark_documented_no_connects

            i2c_role = pin_name_i2c_role(name)
            spi_role = pin_name_spi_role(comp, name) if i2c_role is None else None
            serial_role = (
                pin_name_serial_role(name)
                if i2c_role is None and spi_role is None else None
            )
            select_role = (
                "NSS" if ref in active_spi and pin_name_is_spi_select(name) else None
            )
            if not any((i2c_role, spi_role, serial_role, select_role)):
                if _config_pin_may_float(comp, name, node, ir):
                    continue
                if node in nc and not on_net:
                    continue  # explicit NC on a non-bus pin
                continue

            net_protocols = protocols.get(on_net or "", set())
            role_label = (
                f"I2C_{i2c_role}" if i2c_role
                else f"SPI_{spi_role or select_role}" if spi_role or select_role
                else f"{_serial_protocol(comp, net_protocols)}_{serial_role}"
            )
            if ref not in controllers:
                has_functional_peripheral = True

            if node in nc:
                # A controller can expose several optional hardware buses.
                # Explicitly NC is a complete, intentional state for an
                # unused controller interface (for example ESP32 module
                # flash/SPI contacts).  A peripheral's named bus pin is part
                # of the job that component exists to do and remains an
                # error when marked NC.
                if ref in controllers:
                    continue
                issues.append(_issue(
                    "functional_pin_marked_nc",
                    "error",
                    f"{ref}.{pin.number}",
                    f"{ref}.{pin.number} ({name}) is {role_label} but marked NC",
                ))
                continue

            if not on_net:
                if _config_pin_may_float(comp, name, node, ir):
                    continue
                issues.append(_issue(
                    "functional_pin_unbound",
                    "error",
                    f"{ref}.{pin.number}",
                    f"{ref}.{pin.number} ({name}) is {role_label} but on no net",
                ))

    required_contracts = [
        contract for contract in ir.interface_contracts if contract.required
    ]
    controller_contracts = [
        contract for contract in ir.interface_contracts
        if contract.required and contract.peer == "controller"
    ]
    needs_controller = (
        ir.controller_required or bool(controller_contracts)
        # Legacy/unplanned IR has no typed peer information. Preserve the
        # conservative gate there; a typed sensor breakout may explicitly
        # say its I2C peer is external and must not be forced to contain an
        # on-board controller.
        or (
            has_functional_peripheral
            and ir.controller_required is None
            and not ir.interface_contracts
        )
    )
    if needs_controller and not controllers:
        issues.append(_issue(
            "functional_controller_missing",
            "error",
            "circuit",
            "required functional interface exists but CircuitIR declares no controller",
        ))

    net_by_name = {net.name: net for net in ir.nets}
    for contract in required_contracts:
        net = net_by_name.get(contract.net)
        if net is None:
            issues.append(_issue(
                "functional_interface_net_missing",
                "error",
                f"net:{contract.net}",
                f"required {contract.protocol} interface {contract.net} has no net",
            ))
            continue
        members = {ref for ref, _ in net.nodes}
        if contract.owner_group and not any(
            ir.components.get(ref) is not None
            and ir.components[ref].group == contract.owner_group
            for ref in members
        ):
            issues.append(_issue(
                "functional_interface_missing_owner",
                "error",
                f"net:{net.name}",
                f"{contract.owner_group} declares {net.name} but has no endpoint on it",
            ))
        if contract.peer == "controller" and controllers and not (members & controllers):
            issues.append(_issue(
                "functional_interface_missing_peer",
                "error",
                f"net:{net.name}",
                f"required {contract.protocol} interface {net.name} does not reach "
                f"controller {', '.join(sorted(controllers))}",
            ))
        elif contract.peer == "block" and contract.owner_group and not any(
            ir.components.get(ref) is not None
            and ir.components[ref].group
            and ir.components[ref].group != contract.owner_group
            for ref in members
        ):
            issues.append(_issue(
                "functional_interface_missing_peer",
                "error",
                f"net:{net.name}",
                f"required {contract.protocol} interface {net.name} does not reach "
                "another functional block",
            ))
        elif contract.peer == "external" and not any(
            ir.components.get(ref) is not None
            and ir.components[ref].lib_id.split(":", 1)[0].startswith("Connector")
            for ref in members
        ):
            issues.append(_issue(
                "functional_interface_missing_peer",
                "error",
                f"net:{net.name}",
                f"required external interface {net.name} has no connector endpoint",
            ))

    if not controllers:
        return issues

    for net in ir.nets:
        members = {r for r, _ in net.nodes}

        i2c = is_i2c_net(ir, symbols, net)
        spi = is_spi_net(ir, symbols, net)
        serial_role = None
        serial_kind = "SERIAL"
        # Peripheral-only interface: at least one non-hub member carries a
        # line role in its symbol pin name.  UART has no shared bus detector
        # or normalizer; it still needs this layer-A gate so a TX/RX pin on a
        # peripheral-only net cannot look connected merely because it has a
        # net label.
        has_peripheral = False
        for ref, pin_no in net.nodes:
            if ref in controllers:
                continue
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None:
                continue
            try:
                pname = sym.pin(str(pin_no)).name or ""
            except KeyError:
                continue
            if i2c and pin_name_i2c_role(pname):
                has_peripheral = True
                break
            if spi:
                role = spi_line_role(ir, symbols, net)
                if role and role != "NSS" and pin_name_spi_role(comp, pname):
                    has_peripheral = True
                    break
                if role == "NSS" and pin_name_is_spi_select(pname):
                    has_peripheral = True
                    break
            serial_role = pin_name_serial_role(pname)
            if serial_role:
                serial_kind = _serial_protocol(comp, protocols.get(net.name, set()))
                has_peripheral = True
                break
        if not has_peripheral:
            continue
        if members & controllers:
            if serial_role and serial_kind == "UART":
                issues.extend(_uart_controller_af_issues(
                    ir, symbols, net, serial_role
                ))
            continue
        kind = "I2C" if i2c else "SPI" if spi else serial_kind
        line = (
            i2c_line_role(ir, symbols, net)
            or spi_line_role(ir, symbols, net)
            or serial_role
            or "?"
        )
        issues.append(_issue(
            "functional_bus_missing_controller",
            "error",
            f"net:{net.name}",
            f"{kind} net {net.name} ({line}) does not reach controller "
            f"{', '.join(sorted(controllers))}",
        ))

    return issues
