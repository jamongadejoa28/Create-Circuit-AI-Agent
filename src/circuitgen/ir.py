"""Circuit IR — the structured intermediate representation.

This is what the LLM will eventually produce (as schema-constrained JSON)
and what every deterministic stage (ERC, placement, emission, netlist)
consumes. Kept deliberately small for Phase 1; hierarchy/Bus arrive in
Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pins import PinType


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
        raise KeyError(f"{self.lib_id} has no pin {number!r}")

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


@dataclass
class Component:
    ref: str  # "R1", "D1", "#PWR01"
    lib_id: str
    value: str
    footprint: str = ""


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


@dataclass
class CircuitIR:
    name: str
    components: dict[str, Component] = field(default_factory=dict)
    nets: list[Net] = field(default_factory=list)
    nc_pins: list[tuple[str, str]] = field(default_factory=list)  # explicit no-connects

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
