"""Circuit IR — the structured intermediate representation.

This is what the LLM will eventually produce (as schema-constrained JSON)
and what every deterministic stage (ERC, placement, emission, netlist)
consumes. It stays deliberately small; hierarchy is derived downstream and
functional interfaces are represented as typed contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .pins import PinType

_PIN_ALNUM = re.compile(r"[^A-Za-z0-9]")


@dataclass
class PinDef:
    """A pin as defined in a library symbol (local coordinates, mm)."""

    number: str
    name: str
    etype: PinType
    x: float
    y: float
    orientation: int  # 0/90/180/270, direction the pin points (toward the body)
    length: float
    hidden: bool = False
    unit: int = 1


@dataclass
class SymbolDef:
    """A library symbol: verbatim s-expression + parsed pin/metadata."""

    lib_id: str  # e.g. "Device:R"
    raw_sexp: str  # complete (symbol "..." ...) block for lib_symbols embedding
    pins: list[PinDef] = field(default_factory=list)
    is_power: bool = False
    reference_prefix: str = "U"
    properties: dict[str, str] = field(default_factory=dict)  # Description, ki_keywords, ...

    def pin(self, number: str) -> PinDef:
        for p in self.pins:
            if p.number == str(number):
                return p
            # KiCad 10 represents coincident physical pins with a bundled
            # number such as ``[1,15,38,39]``.  A request naturally names
            # physical pin 38; it still addresses this graphical pin.
            if p.number.startswith("[") and p.number.endswith("]") and str(number) in {
                item.strip() for item in p.number[1:-1].split(",")
            }:
                return p
        raise KeyError(f"{self.lib_id} has no pin {number!r}")

    def has_pin(self, number: str) -> bool:
        """Whether `pin()` would succeed. ERC, the repair gate, and the
        drop-unknown-pins pass share this so a phantom C1.3 cannot be a
        net member while the checker calls it unknown."""
        try:
            self.pin(str(number))
        except KeyError:
            return False
        return True

    @property
    def is_source(self) -> bool:
        """CANDIDATE FIX (scratch): a cell/battery — two PASSIVE terminals
        named "+"/"-" (Device:Battery, Device:Solar_Cell)."""
        pins = [p for p in self.pins if not p.hidden]
        if len(pins) != 2 or not all(p.etype == PinType.PASSIVE for p in pins):
            return False
        return {(p.name or "").strip() for p in pins} == {"+", "-"}

    def placed_units(self) -> list[int]:
        """Units that get their own placed instance.

        Single-unit symbols keep their pins in a `_0_1` or `_1_1` block and
        are placed as one instance with (unit 1); multi-unit symbols get
        one instance per non-zero unit (e.g. 74LS00: gates 1-4 + power
        unit 5 — the power unit is a real instance that must be placed and
        wired like any other).
        """
        units = sorted({p.unit for p in self.pins} - {0})
        return units or [1]

    def visible_contacts(self) -> list[PinDef]:
        return [p for p in self.pins if not p.hidden]

    def contact_name_restates_number(self, pin: PinDef) -> bool:
        """True when the name carries no identity beyond the pin number.

        KiCad generic headers are ``Pin_1`` / ``Pin_2``; some symbols repeat
        the number as the name; empty and ``~`` are KiCad's blank. A pin
        named SDA or CC1 is not this.
        """
        name = (pin.name or "").strip()
        if not name or name in ("~", "~{}"):
            return True
        n = _PIN_ALNUM.sub("", name).upper()
        number = _PIN_ALNUM.sub("", pin.number).upper()
        return bool(number) and (n == number or n == "PIN" + number)

    def contacts_are_anonymous(self) -> bool:
        pins = self.visible_contacts()
        return bool(pins) and all(self.contact_name_restates_number(p) for p in pins)

    def next_free_contact_number(self, occupied: set[str]) -> str | None:
        """Lowest unused visible pin number. NC is not occupancy — a later
        pass drops stale NC markers once the pin is on a net."""
        def key(num: str) -> tuple:
            return (0, int(num)) if str(num).isdigit() else (1, str(num))

        free = sorted(
            (p.number for p in self.visible_contacts() if p.number not in occupied),
            key=key,
        )
        return free[0] if free else None


@dataclass
class Component:
    ref: str  # "R1", "D1", "#PWR01"
    lib_id: str
    value: str
    footprint: str = ""
    # Deterministic functional-block ownership.  The LLM does not emit this;
    # instantiate_blocks stamps it so placement and visual QA can keep a
    # board-scale schematic readable.
    group: str = ""
    # Why an exact requested part could not be bound to the catalog symbol.
    # This survives draft emission/JSON recording so a pin-map conflict is a
    # structured product error, not a line that disappears in the run log.
    binding_error: str = ""


@dataclass
class Net:
    name: str
    nodes: list[tuple[str, str]] = field(default_factory=list)  # (ref, pin number)


@dataclass
class ValidationIssue:
    checker: str  # "circuitgen-erc" | "kicad-erc" | ...
    rule: str  # e.g. "pin_conflict", "single_pin_net"
    severity: str  # "error" | "warning"
    path: str  # object path, e.g. "R1.2" or "net:LED_K"
    message: str


@dataclass(frozen=True)
class InterfaceContract:
    """A required functional connection preserved from the block plan.

    ``owner_group`` identifies the block instance that exposes ``net``;
    ``peer`` says which kind of endpoint must share it.  This is deliberately
    topology metadata rather than a pin-name guess: a motor driver's PWM line
    and an I2C sensor's SDA line are both controller contracts even though
    only one resembles a named protocol.
    """

    net: str
    owner_group: str = ""
    peer: str = "controller"  # controller | external | block
    protocol: str = "other"  # i2c | spi | uart | can | generic_control | other
    purpose: str = ""
    required: bool = True


@dataclass
class CircuitIR:
    name: str
    components: dict[str, Component] = field(default_factory=dict)
    nets: list[Net] = field(default_factory=list)
    nc_pins: list[tuple[str, str]] = field(default_factory=list)  # explicit no-connects
    # Controller identity and functional endpoint contracts are authored by
    # the requirement/block stages and must survive every deterministic pass.
    # Correctness checks must not rediscover the controller from pin count.
    # None means legacy/untyped IR (the checker conservatively infers intent
    # from functional pins). False is an explicit design/transcription fact:
    # this circuit intentionally has no on-board controller.
    controller_required: bool | None = None
    controller_refs: list[str] = field(default_factory=list)
    interface_contracts: list[InterfaceContract] = field(default_factory=list)

    def add(self, comp: Component) -> Component:
        if comp.ref in self.components:
            raise ValueError(f"duplicate reference {comp.ref}")
        self.components[comp.ref] = comp
        return comp

    def connect(self, net_name: str, *nodes: tuple[str, str]) -> Net:
        for net in self.nets:
            if net.name == net_name:
                net.nodes.extend(nodes)
                return net
        net = Net(name=net_name, nodes=list(nodes))
        self.nets.append(net)
        return net
