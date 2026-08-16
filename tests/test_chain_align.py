"""Chain alignment: series/filter passives on 2-node nets must come out as
REAL wires (user requirement: analog circuits fully drawn), and the moved
placement must stay electrically identical (oracle round-trip)."""

import re
from pathlib import Path

import pytest

from circuitgen.emit import emit_schematic
from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import KICAD_CLI, export_netlist, run_erc
from circuitgen.netlist import compare_connectivity
from circuitgen.place import heuristic_place
from circuitgen.project import write_project
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "chain"


def _board() -> CircuitIR:
    """Op-amp with a series input R, feedback R, and an output RC filter."""
    ir = CircuitIR("chain_t")
    ir.add(Component("U1", "Amplifier_Operational:MCP6001-OT", "MCP6001",
                     "Package_TO_SOT_SMD:SOT-23-5", "ANALOG"))
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "ANALOG"))
    ir.add(Component("R2", "Device:R", "100k", "Resistor_SMD:R_0603_1608Metric", "ANALOG"))
    ir.add(Component("R3", "Device:R", "47R", "Resistor_SMD:R_0603_1608Metric", "ANALOG"))
    ir.add(Component("C1", "Device:C", "100nF", "Capacitor_SMD:C_0603_1608Metric", "ANALOG"))
    ir.add(Component("C2", "Device:C", "1nF", "Capacitor_SMD:C_0603_1608Metric", "ANALOG"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x02", "AIN",
                     "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "ANALOG"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.add(Component("#FLG01", "power:PWR_FLAG", "PWR_FLAG"))
    ir.add(Component("#FLG02", "power:PWR_FLAG", "PWR_FLAG"))
    ir.connect("+3V3", ("U1", "5"), ("C1", "1"), ("#PWR01", "1"), ("#FLG01", "1"))
    ir.connect("GND", ("U1", "2"), ("C1", "2"), ("C2", "2"), ("J1", "2"),
               ("#PWR02", "1"), ("#FLG02", "1"))
    ir.connect("AIN", ("J1", "1"), ("R1", "1"))
    ir.connect("INP", ("R1", "2"), ("U1", "3"))
    ir.connect("FB", ("R2", "2"), ("U1", "4"))
    ir.connect("OUT", ("U1", "1"), ("R2", "1"), ("R3", "1"))
    ir.connect("AOUT", ("R3", "2"), ("C2", "1"))
    return ir


def test_chains_become_real_wires_and_roundtrip():
    ir = _board()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()}))
    placements = heuristic_place(ir, symbols)
    text = emit_schematic(ir, symbols, placements)

    wires = re.findall(
        r"\(wire\s*\(pts\s*\(xy ([\d.-]+) ([\d.-]+)\)\s*\(xy ([\d.-]+) ([\d.-]+)\)", text
    )
    routed = sum(
        1 for x1, y1, x2, y2 in wires
        if abs(abs(float(x2) - float(x1)) + abs(float(y2) - float(y1)) - 7.62) > 0.01
    )
    # INP, FB, AOUT chains at minimum; AIN may route too depending on J1
    assert routed >= 3, f"expected >=3 routed wires, got {routed}"

    OUT.mkdir(parents=True, exist_ok=True)
    sch = OUT / "chain_t.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    write_project(sch)

    erc = run_erc(sch)
    assert erc.ok, [v.get("type") for v in erc.violations]

    net = OUT / "chain_t.net"
    assert export_netlist(sch, net).returncode == 0
    ok, msg = compare_connectivity(ir, net)
    assert ok, msg
