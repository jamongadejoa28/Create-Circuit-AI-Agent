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

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "hier"


def _board() -> CircuitIR:
    """Three groups that each genuinely earn a sheet: two multi-pin devices
    apiece. A group holding one device plus passives now shares the hub's
    sheet, so a board built from those would be a ONE-sheet board and could
    not exercise the hierarchy at all. The devices are all the same I2C sensor
    because this test is about sheet mechanics — ownership, ports, round-trip
    — not about the circuit being a sensible product.
    """
    ir = CircuitIR("hier_t")
    sensor = ("Sensor_Temperature:Si7050-A20", "Si7050",
              "Package_DFN_QFN:DFN-6-1EP_3x3mm_P1mm_EP1.5x2.4mm")
    devices = {"POWER": ["U5", "U6"], "SENSOR": ["U1", "U2"], "MCU": ["U3", "U4"]}
    for group, refs in devices.items():
        for ref in refs:
            ir.add(Component(ref, *sensor, group))
    ir.add(Component("C1", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric", "POWER"))
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("R3", "Device:R", "330R", "Resistor_SMD:R_0603_1608Metric", "MCU"))
    ir.add(Component("D1", "Device:LED", "LED", "LED_SMD:LED_0603_1608Metric", "MCU"))
    ir.add(Component("R4", "Device:R", "1k", "Resistor_SMD:R_0603_1608Metric", "MCU"))
    ir.add(Component("C2", "Device:C", "10nF", "Capacitor_SMD:C_0603_1608Metric", "MCU"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    all_sensors = [r for refs in devices.values() for r in refs]
    ir.connect("+3V3", ("C1", "1"), ("R1", "1"), ("R2", "1"), ("R3", "1"),
               ("R4", "1"), ("#PWR01", "1"), *[(r, "5") for r in all_sensors])
    ir.connect("GND", ("C1", "2"), ("D1", "1"), ("C2", "2"), ("#PWR02", "1"),
               *[(r, "2") for r in all_sensors])
    ir.connect("SDA", ("R1", "2"), ("D1", "2"), *[(r, "1") for r in all_sensors])
    ir.connect("SCL", ("R2", "2"), ("R3", "2"), *[(r, "6") for r in all_sensors])
    # sheet-local net: KiCad exports it path-prefixed ("/MCU/FILT") and the
    # round-trip must resolve that back to the IR name
    ir.connect("FILT", ("R4", "2"), ("C2", "1"))
    ir.nc_pins = [(r, p) for r in all_sensors for p in ("3", "4")]
    return ir


def test_hierarchical_emission_erc_clean_and_roundtrips():
    ir = _board()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    partition = partition_by_function(ir, symbols)
    assert set(partition) == {"POWER", "SENSOR", "MCU"}, sorted(partition)

    res = emit_hierarchical(ir, symbols, partition, OUT, "hier_t", None)
    assert len(res["children"]) == 3
    write_project(res["root"])

    erc = run_erc(res["root"])
    assert erc.ok, [v.get("type") for v in erc.violations]

    exported = OUT / "hier_t.kicad-export.net"
    assert export_netlist(res["root"], exported).returncode == 0
    ok, msg = compare_connectivity(ir, exported)
    assert ok, msg

    # A net that leaves the sheet does so through a hierarchical label paired
    # with a pin on the root's sheet symbol. Global labels are positionless,
    # which left the root a row of empty rectangles: no pins, no wires, every
    # name already printed on the sheet it pointed at.
    sensor_text = res["children"]["SENSOR"].read_text()
    assert '(hierarchical_label "SDA"' in sensor_text
    root_text = res["root"].read_text()
    assert '(pin "SDA" bidirectional' in root_text
    assert root_text.count("(wire") >= 2, "the root must draw the interconnect"
    assert "(junction" in root_text, "a stub meeting a trunk needs a junction"
    assert 'power:+3V3' in sensor_text
    # exactly one PWR_FLAG instance per rail project-wide (PWROUT x PWROUT otherwise)
    flags = sum(
        child.read_text().count('(lib_id "power:PWR_FLAG")')
        for child in res["children"].values()
    )
    assert flags == 2  # +3V3 and GND, each flagged once
