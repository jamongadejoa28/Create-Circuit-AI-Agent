#!/usr/bin/env python3
"""Build the golden reference schematic (plan §9 Phase 1 / golden circuit 1).

LED + current-limit resistor + push button between +5V and GND, with
hand-chosen placements. Run from the repo root:

    PYTHONPATH=src .venv/bin/python scripts/make_golden.py

Writes golden/golden_led_button.kicad_sch (+ .kicad_pro) and runs
kicad-cli ERC on it. Exit code 0 only on a clean ERC.
"""

import sys
from pathlib import Path

from circuitgen.emit import emit_schematic
from circuitgen.geometry import Placement
from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import export_svg, run_erc
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.project import write_project
from circuitgen.symbols import load_symbols

OUT_DIR = Path(__file__).resolve().parent.parent / "golden"


def build_ir() -> CircuitIR:
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


# Hand layout: signal flow left→right on one row, supplies at the ends.
PLACEMENTS = {
    "SW1": Placement(63.5, 63.5, 0),  # pins at (58.42, 63.5) / (68.58, 63.5)
    "R1": Placement(82.55, 63.5, 270),  # horizontal, pin1 left (78.74) pin2 right (86.36)
    "D1": Placement(95.25, 63.5, 180),  # anode left (91.44), cathode right (99.06)
    "#PWR01": Placement(53.34, 60.96, 0),  # +5V, stub drops to y=63.5
    "#PWR02": Placement(105.41, 66.04, 0),  # GND, stub rises to y=63.5
    "#FLG01": Placement(48.26, 60.96, 0),  # PWR_FLAG on +5V
    "#FLG02": Placement(110.49, 66.04, 0),  # PWR_FLAG on GND
}


def main() -> int:
    ir = build_ir()
    symbols = load_symbols(
        [c.lib_id for c in ir.components.values()] + ["power:PWR_FLAG"]
    )
    added = ensure_pwr_flags(ir, symbols)
    print(f"PWR_FLAGs added: {added}")

    OUT_DIR.mkdir(exist_ok=True)
    sch_path = OUT_DIR / "golden_led_button.kicad_sch"
    sch_path.write_text(emit_schematic(ir, symbols, PLACEMENTS), encoding="utf-8")
    write_project(sch_path)
    print(f"wrote {sch_path}")

    result = run_erc(sch_path)
    print(f"ERC exit={result.exit_code} violations={len(result.violations)}")
    for v in result.violations:
        print(f"  [{v.get('severity')}] {v.get('type')}: {v.get('description')}")
        for item in v.get("items", []):
            print(f"      - {item.get('description')} @ {item.get('pos')}")
    if result.stderr:
        print("stderr:", result.stderr)

    svg = export_svg(sch_path, OUT_DIR / "svg")
    print(f"SVG export exit={svg.returncode}")

    return 0 if result.ok and svg.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
