"""Multi-unit IC support, end to end against kicad-cli (Phase 2 gate).

74xx:74LS00 = four NAND gates (units 1-4) + a power unit (unit 5, pins
7/14). One gate drives an LED; unused gate inputs are tied to GND and
their outputs are explicitly no-connected — the standard discipline the
extended MCU ERC (Phase 3) will also demand.
"""

from pathlib import Path

import pytest

from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated"


def nand_led_ir() -> CircuitIR:
    ir = CircuitIR(name="nand_led")
    ir.add(Component("U1", "74xx:74LS00", "74LS00", "Package_DIP:DIP-14_W7.62mm"))
    ir.add(Component("R1", "Device:R", "330R", "Resistor_SMD:R_0805_2012Metric"))
    ir.add(Component("D1", "Device:LED", "LED", "LED_SMD:LED_0805_2012Metric"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    ir.connect("+5V", ("U1", "1"), ("U1", "2"), ("U1", "14"), ("#PWR01", "1"))
    ir.connect("OUT", ("U1", "3"), ("R1", "1"))
    ir.connect("R_LED", ("R1", "2"), ("D1", "2"))
    ir.connect("GND", ("D1", "1"), ("U1", "7"), ("#PWR02", "1"),
               # unused gate inputs tied low
               ("U1", "4"), ("U1", "5"), ("U1", "9"), ("U1", "10"), ("U1", "12"), ("U1", "13"))
    ir.nc_pins = [("U1", "6"), ("U1", "8"), ("U1", "11")]  # unused gate outputs
    return ir


def test_74ls00_symbol_shape():
    sym = load_symbols(["74xx:74LS00"])["74xx:74LS00"]
    assert sym.placed_units() == [1, 2, 3, 4, 5]
    # De Morgan body-style pins must not be double-collected
    assert len(sym.pins) == 14
    by_unit = {}
    for p in sym.pins:
        by_unit.setdefault(p.unit, []).append(p.number)
    assert sorted(by_unit[5]) == ["14", "7"]  # power unit


def test_nand_led_pipeline_clean():
    res = generate(nand_led_ir(), OUT / "nand")
    assert res.errors == [], res.errors
    # the fixture deliberately omits a decoupling cap: the §8.2 lint must
    # notice (warning — does not gate the pipeline, feeds the repair loop)
    assert any(i.rule == "decoupling_missing" for i in res.self_erc)
    assert res.kicad_erc.ok, [v.get("type") for v in res.kicad_erc.violations]
    assert res.connectivity_ok, res.connectivity_msg
    assert res.svg_ok
    # 5 unit instances for U1 -> the file must contain (unit 5) etc.
    text = res.sch_path.read_text()
    for unit in (1, 2, 3, 4, 5):
        assert f"(unit {unit})" in text


def test_multiunit_needs_per_unit_placements():
    from circuitgen.emit import emit_schematic
    from circuitgen.geometry import Placement

    ir = nand_led_ir()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    flat = {ref: Placement(50.8 + i * 30.48, 50.8) for i, ref in enumerate(ir.components)}
    with pytest.raises(ValueError, match="units"):
        emit_schematic(ir, symbols, flat)
