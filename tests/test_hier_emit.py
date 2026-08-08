"""Hierarchical emission against the real KiCad oracle.

Synthetic 3-sheet board (POWER / SENSOR / MCU_CAN_DEBUG) with cross-sheet
I2C signals: root must resolve all children, ERC must be clean, and the
netlist round-trip must reproduce the full flat IR connectivity."""

from pathlib import Path

import pytest

from circuitgen.hier_emit import emit_hierarchical
from circuitgen.hierarchy import partition_by_function
from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import KICAD_CLI, export_netlist, run_erc
from circuitgen.netlist import compare_connectivity
from circuitgen.project import write_project
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent.parent / "out" / "tests" / "hier"


def _board() -> CircuitIR:
    ir = CircuitIR("hier_t")
    ir.add(Component("C1", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "POWER"))
    ir.add(Component("U1", "Sensor_Temperature:Si7050-A20", "Si7050",
                     "Package_DFN_QFN:DFN-6-1EP_3x3mm_P1mm_EP1.5x2.4mm", "SENSOR"))
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("R3", "Device:R", "330R", "Resistor_SMD:R_0603_1608Metric", "MCU"))
    ir.add(Component("D1", "Device:LED", "LED", "LED_SMD:LED_0603_1608Metric", "MCU"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+3V3", ("C1", "1"), ("U1", "5"), ("R1", "1"), ("R2", "1"), ("R3", "1"), ("#PWR01", "1"))
    ir.connect("GND", ("C1", "2"), ("U1", "2"), ("D1", "1"), ("#PWR02", "1"))
    ir.connect("SDA", ("U1", "1"), ("R1", "2"), ("D1", "2"))
    ir.connect("SCL", ("U1", "6"), ("R2", "2"), ("R3", "2"))
    ir.nc_pins = [("U1", "3"), ("U1", "4")]
    return ir


def test_hierarchical_emission_erc_clean_and_roundtrips():
    ir = _board()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    partition = partition_by_function(ir)
    assert {"POWER", "SENSOR", "MCU_CAN_DEBUG"} <= set(partition)

    res = emit_hierarchical(ir, symbols, partition, OUT, "hier_t", None)
    assert len(res["children"]) == 3
    write_project(res["root"])

    erc = run_erc(res["root"])
    assert erc.ok, [v.get("type") for v in erc.violations]

    exported = OUT / "hier_t.kicad-export.net"
    assert export_netlist(res["root"], exported).returncode == 0
    ok, msg = compare_connectivity(ir, exported)
    assert ok, msg

    # cross-sheet signals are global labels; rails are power symbols per sheet
    sensor_text = res["children"]["SENSOR"].read_text()
    assert '(global_label "SDA"' in sensor_text
    assert 'power:+3V3' in sensor_text
    # exactly one PWR_FLAG instance per rail project-wide (PWROUT x PWROUT otherwise)
    flags = sum(
        child.read_text().count('(lib_id "power:PWR_FLAG")')
        for child in res["children"].values()
    )
    assert flags == 2  # +3V3 and GND, each flagged once
