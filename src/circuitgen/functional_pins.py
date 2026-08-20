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
    pin_name_is_chip_select,
    pin_name_spi_role,
    pin_name_uart_role,
    spi_line_role,
)
from .ir import CircuitIR, SymbolDef, ValidationIssue
from .normalize import hub_ref
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


def check_functional_pin_completeness(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[ValidationIssue]:
    """Every interface-named pin is CONNECTED, EXPLICIT_NC, or allowed FLOAT.

    Uses the same pin-name facts as ``i2c_member_role`` and ``spi_line_role``,
    not a per-part denylist. A TMP100 SDA pin and a Si7051 SDA pin are the
    same check.
    """
    issues: list[ValidationIssue] = []
    pin_net = _pin_net_map(ir)
    nc = {(r, str(p)) for r, p in ir.nc_pins}

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

            bus_role = pin_name_i2c_role(name) or pin_name_spi_role(comp, name)
            uart_role = pin_name_uart_role(name) if bus_role is None else None
            if bus_role is None and uart_role is None:
                if _config_pin_may_float(comp, name, node, ir):
                    continue
                if node in nc and not on_net:
                    continue  # explicit NC on a non-bus pin
                continue

            role_label = (
                f"I2C_{bus_role}" if bus_role and bus_role in ("SDA", "SCL")
                else f"SPI_{bus_role}" if bus_role
                else f"UART_{uart_role}"
            )

            if node in nc and (bus_role or uart_role):
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

    hub = hub_ref(ir, symbols)
    if hub is None:
        return issues

    for net in ir.nets:
        if not is_i2c_net(ir, symbols, net) and not is_spi_net(ir, symbols, net):
            continue
        members = {r for r, _ in net.nodes}
        if hub in members:
            continue
        # Peripheral-only bus: at least one member carries the line role.
        has_peripheral = False
        for ref, pin_no in net.nodes:
            if ref == hub:
                continue
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None:
                continue
            try:
                pname = sym.pin(str(pin_no)).name or ""
            except KeyError:
                continue
            if is_i2c_net(ir, symbols, net) and pin_name_i2c_role(pname):
                has_peripheral = True
                break
            if is_spi_net(ir, symbols, net):
                role = spi_line_role(ir, symbols, net)
                if role and role != "NSS" and pin_name_spi_role(comp, pname):
                    has_peripheral = True
                    break
                if role == "NSS" and pin_name_is_chip_select(pname):
                    has_peripheral = True
                    break
        if not has_peripheral:
            continue
        kind = "I2C" if is_i2c_net(ir, symbols, net) else "SPI"
        line = i2c_line_role(ir, symbols, net) or spi_line_role(ir, symbols, net) or "?"
        issues.append(_issue(
            "functional_bus_missing_hub",
            "error",
            f"net:{net.name}",
            f"{kind} net {net.name} ({line}) has no controller ({hub})",
        ))

    return issues
