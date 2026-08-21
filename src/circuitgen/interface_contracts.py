"""Typed interface contracts authored at RequirementSpec time.

BlockPlan partition may expand ``{n}`` and attach ``owner_group``, but it must
not be the only place contracts exist. Flat synthesis (``parts_needed`` below
the block threshold) needs the same ``InterfaceContract`` correctness model
for PWM/DIR/FAULT and other generic control nets that pin-name heuristics
cannot classify.
"""

from __future__ import annotations

from .ir import CircuitIR, InterfaceContract

_PEERS = frozenset({"controller", "external", "block"})
_PROTOCOLS = frozenset({
    "i2c", "spi", "uart", "can", "generic_control", "other",
})


def _signal_fields(sig: dict) -> InterfaceContract | None:
    name = str(sig.get("name", "")).strip()
    if not name:
        return None
    peer = str(sig.get("peer") or "external").strip().lower()
    if peer not in _PEERS:
        peer = "external"
    protocol = str(sig.get("protocol") or "other").strip().lower()
    if protocol not in _PROTOCOLS:
        protocol = "other"
    return InterfaceContract(
        net=name,
        owner_group="",
        peer=peer,
        protocol=protocol,
        purpose=str(sig.get("purpose") or ""),
        required=bool(sig.get("required", True)),
    )


def interface_contracts_from_spec(spec: dict | None) -> list[InterfaceContract]:
    """Materialize RequirementSpec.signals as CircuitIR contracts.

    Missing peer defaults to ``external`` (board-exposed nets — the historical
    meaning of ``signals``). The extractor must set ``peer=controller`` for
    MCU-bound control lines; this function does not invent protocol from
    net-name tokens.
    """
    out: list[InterfaceContract] = []
    seen: set[str] = set()
    for sig in (spec or {}).get("signals") or []:
        if not isinstance(sig, dict):
            continue
        contract = _signal_fields(sig)
        if contract is None or contract.net in seen:
            continue
        seen.add(contract.net)
        out.append(contract)
    return out


def merge_interface_contracts(
    *groups: list[InterfaceContract],
) -> list[InterfaceContract]:
    """Union contracts by net name.

    Prefer an entry that already has ``owner_group`` (block instantiation)
    over a spec-floor entry with the same net. Later groups fill gaps only.
    """
    by_net: dict[str, InterfaceContract] = {}
    for group in groups:
        for contract in group:
            existing = by_net.get(contract.net)
            if existing is None:
                by_net[contract.net] = contract
                continue
            if not existing.owner_group and contract.owner_group:
                by_net[contract.net] = contract
    return list(by_net.values())


def apply_spec_interface_contracts(ir: CircuitIR, spec: dict | None) -> list[str]:
    """Attach/merge spec-authored contracts onto ``ir``. Returns log notes."""
    floor = interface_contracts_from_spec(spec)
    if not floor and not ir.interface_contracts:
        return []
    before = len(ir.interface_contracts)
    ir.interface_contracts = merge_interface_contracts(
        list(ir.interface_contracts), floor
    )
    added = len(ir.interface_contracts) - before
    if added <= 0 and not floor:
        return []
    names = ", ".join(c.net for c in floor) if floor else "(none)"
    return [f"interface contracts from spec.signals: {names}"]


def signal_as_interface_net(sig: dict) -> dict:
    """BLOCK_PLAN interface_nets row derived from a typed RequirementSpec signal."""
    contract = _signal_fields(sig)
    if contract is None:
        raise ValueError("signal missing name")
    return {
        "name": contract.net,
        "purpose": (contract.purpose or "")[:60],
        "peer": contract.peer,
        "protocol": contract.protocol,
        "required": contract.required,
    }


def reconcile_plan_interfaces(plan: list[dict], spec: dict) -> list[str]:
    """Ensure every typed spec signal appears on some block's interface_nets.

    Planner output may invent additional nets; those stay. Spec signals that
    were dropped are restored onto the block that owns ``owner_role``, else a
    controller-role block for ``peer=controller``, else the first block.
    """
    notes: list[str] = []
    if not plan:
        return notes

    def declared_names() -> set[str]:
        names: set[str] = set()
        for block in plan:
            for net in block.get("interface_nets") or []:
                raw = str(net.get("name", "")).strip()
                if not raw:
                    continue
                names.add(raw)
                if "{n}" in raw:
                    for inst in range(1, int(block.get("count", 1)) + 1):
                        names.add(raw.replace("{n}", str(inst)))
        return names

    role_to_block: dict[str, dict] = {}
    for block in plan:
        for role in block.get("roles") or []:
            role_to_block.setdefault(str(role), block)

    controller_block = next(
        (
            block for block in plan
            if any(
                str(p.get("role")) in (block.get("roles") or [])
                and p.get("functional_kind") == "microcontroller"
                for p in spec.get("parts_needed") or []
            )
        ),
        None,
    )
    if controller_block is None:
        controller_block = next(
            (
                block for block in plan
                if any(
                    "mcu" in str(r).lower() or "controller" in str(r).lower()
                    for r in block.get("roles") or []
                )
            ),
            plan[0],
        )

    declared = declared_names()
    for sig in spec.get("signals") or []:
        if not isinstance(sig, dict):
            continue
        name = str(sig.get("name", "")).strip()
        if not name or name in declared:
            continue
        owner_role = str(sig.get("owner_role") or "").strip()
        target = role_to_block.get(owner_role) if owner_role else None
        peer = str(sig.get("peer") or "external").strip().lower()
        if target is None and peer == "controller":
            # Peripheral-facing lines live on the peripheral block, not MCU.
            target = next(
                (b for b in plan if b is not controller_block),
                controller_block,
            )
        if target is None:
            target = plan[0]
        target.setdefault("interface_nets", []).append(signal_as_interface_net(sig))
        declared.add(name)
        notes.append(
            f"reconciled spec signal {name} into block {target.get('id')}"
        )
    return notes


def catalog_from_contracts(contracts: list[InterfaceContract]) -> list[dict]:
    """Shape used by ``Agent.wire_mcu_interfaces``."""
    return [
        {
            "net": c.net,
            "purpose": (c.purpose or "")[:40],
            "block": c.owner_group or "CIRCUIT",
            "peer": c.peer,
            "protocol": c.protocol,
            "required": c.required,
        }
        for c in contracts
    ]


def bus_line_rank(
    ir: CircuitIR,
    net_name: str,
    symbols: dict | None,
) -> int:
    """Within a bus tier, route clock before data before chip-select.

    Lower sorts first. Non-bus nets use a large rank so terminal count
    remains the only secondary key among ordinary nets.
    """
    for contract in ir.interface_contracts:
        if contract.net != net_name or not contract.required:
            continue
        if contract.protocol == "generic_control":
            return 0
        if contract.protocol == "i2c":
            return 1
        if contract.protocol == "spi":
            return 1
        if contract.protocol in {"uart", "can"}:
            return 1
        return 2
    if symbols is None:
        return 50
    net = next((n for n in ir.nets if n.name == net_name), None)
    if net is None:
        return 50
    from .erc import (
        i2c_line_role,
        is_i2c_net,
        is_spi_net,
        pin_name_is_spi_select,
        spi_line_role,
    )

    if is_i2c_net(ir, symbols, net):
        role = i2c_line_role(ir, symbols, net)
        return {"SCL": 0, "SDA": 1}.get(role or "", 2)
    if is_spi_net(ir, symbols, net):
        role = spi_line_role(ir, symbols, net)
        return {"SCK": 0, "MOSI": 1, "MISO": 2, "NSS": 3}.get(role or "", 4)
    # Chip-select on an active SPI peripheral is not always classified as an
    # SPI bus line (GPIO CS). Still schedule it after clock/data.
    for ref, pin_no in net.nodes:
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        if comp is None or sym is None:
            continue
        try:
            pname = sym.pin(str(pin_no)).name or ""
        except KeyError:
            continue
        if not pin_name_is_spi_select(pname):
            continue
        if any(
            is_spi_net(ir, symbols, other)
            for other in ir.nets
            if any(r == ref for r, _ in other.nodes)
        ):
            return 3
    return 50


def routing_net_priority(
    ir: CircuitIR,
    net_name: str,
    symbols: dict | None = None,
) -> tuple[int, int, int, str]:
    """Sort key for emit: lower sorts first.

    Required controller contracts claim board space before ordinary nets.
    When the IR has no typed contracts (legacy saved runs), I2C/SPI buses
    detected from pin membership still route early — same C-layer need,
    not an A-layer normalize rule.
    Within the bus tier: SCK → MOSI → MISO → CS, then terminal count.
    """
    terminals = next(
        (len(net.nodes) for net in ir.nets if net.name == net_name),
        0,
    )
    best = 2  # ordinary
    for contract in ir.interface_contracts:
        if contract.net != net_name or not contract.required:
            continue
        if contract.peer == "controller":
            best = 0
            break
        best = min(best, 1)
    if best == 2 and symbols is not None:
        net = next((n for n in ir.nets if n.name == net_name), None)
        if net is not None:
            from .erc import is_i2c_net, is_spi_net

            if is_i2c_net(ir, symbols, net) or is_spi_net(ir, symbols, net):
                best = 0
            elif bus_line_rank(ir, net_name, symbols) == 3:
                # GPIO CS on an active SPI device — still critical-ish.
                best = 0
    return (best, bus_line_rank(ir, net_name, symbols), -terminals, net_name)
