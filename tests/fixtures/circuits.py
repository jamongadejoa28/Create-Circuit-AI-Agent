"""Small deterministic circuits shared by parser and pipeline tests.

These are test inputs, not design knowledge or model answers. Product code
never imports this module. The circuits exist only to exercise general
invariants such as netlist partitioning, deterministic emission, and KiCad
round-trip connectivity.
"""

from __future__ import annotations

from circuitgen.geometry import Placement
from circuitgen.ir import CircuitIR, Component


def led_button_ir() -> CircuitIR:
    """A minimal series chain with two power nets and three signal parts."""
    ir = CircuitIR(name="led_button_fixture")
    ir.add(Component(
        "SW1", "Switch:SW_Push", "SW_Push",
        "Button_Switch_SMD:SW_SPST_PTS645Sx43SMTR92",
    ))
    ir.add(Component(
        "R1", "Device:R", "330R", "Resistor_SMD:R_0805_2012Metric"
    ))
    ir.add(Component(
        "D1", "Device:LED", "LED", "LED_SMD:LED_0805_2012Metric"
    ))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    ir.connect("+5V", ("SW1", "1"), ("#PWR01", "1"))
    ir.connect("SW_R", ("SW1", "2"), ("R1", "1"))
    ir.connect("R_LED", ("R1", "2"), ("D1", "2"))
    ir.connect("GND", ("D1", "1"), ("#PWR02", "1"))
    return ir


# Fixed coordinates isolate emitter determinism from placement heuristics.
LED_BUTTON_PLACEMENTS: dict[str, Placement] = {
    "SW1": Placement(63.5, 63.5, 0),
    "R1": Placement(82.55, 63.5, 90),
    "D1": Placement(95.25, 63.5, 180),
    "#PWR01": Placement(53.34, 60.96, 0),
    "#PWR02": Placement(105.41, 66.04, 0),
    "#FLG01": Placement(48.26, 60.96, 0),
    "#FLG02": Placement(110.49, 66.04, 0),
}
