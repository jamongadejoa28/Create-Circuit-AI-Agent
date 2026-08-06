"""Reference circuits (plan §10 golden circuits).

Circuit 1 lives here so tests, scripts, and future agent evaluations all
build the exact same IR.
"""

from __future__ import annotations

from .geometry import Placement
from .ir import CircuitIR, Component


def golden_led_button_ir() -> CircuitIR:
    """Golden circuit 1: LED + current-limit resistor + push button."""
    ir = CircuitIR(name="golden_led_button")
    ir.add(Component("SW1", "Switch:SW_Push", "SW_Push", "Button_Switch_SMD:SW_SPST_PTS645"))
    ir.add(Component("R1", "Device:R", "330R", "Resistor_SMD:R_0805_2012Metric"))
    ir.add(Component("D1", "Device:LED", "LED", "LED_SMD:LED_0805_2012Metric"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    # +5V ── SW1 ── R1 ── LED(A→K) ── GND
    ir.connect("+5V", ("SW1", "1"), ("#PWR01", "1"))
    ir.connect("SW_R", ("SW1", "2"), ("R1", "1"))
    ir.connect("R_LED", ("R1", "2"), ("D1", "2"))  # D1.2 = anode
    ir.connect("GND", ("D1", "1"), ("#PWR02", "1"))  # D1.1 = cathode
    return ir


# Hand layout used by the checked-in golden file: signal flow left→right
# on one row, supplies at the ends (see golden/golden_led_button.kicad_sch).
GOLDEN_PLACEMENTS: dict[str, Placement] = {
    "SW1": Placement(63.5, 63.5, 0),
    "R1": Placement(82.55, 63.5, 90),  # rot 90 puts pin 1 on the left (probe-verified)
    "D1": Placement(95.25, 63.5, 180),
    "#PWR01": Placement(53.34, 60.96, 0),
    "#PWR02": Placement(105.41, 66.04, 0),
    "#FLG01": Placement(48.26, 60.96, 0),
    "#FLG02": Placement(110.49, 66.04, 0),
}
