"""Multi-terminal tree routing against the real KiCad oracle.

A 3-node I2C bus (two sensors + pullup per line) must come out as REAL
junctioned wire trees — not stub+label — while keeping ERC clean and the
netlist round-trip identical. This proves the whole safety chain: escape
segments, obstacle blocking, segment splitting at branch points, junction
emission (an unsplit branch would pass ERC yet be electrically dangling)."""

from pathlib import Path

import pytest

from circuitgen.emit import build_emit_plan, emit_schematic, normalize_placements
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

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "tree"


def _board() -> CircuitIR:
    ir = CircuitIR("tree_t")
    for ref in ("U1", "U2"):
        ir.add(Component(ref, "Sensor_Temperature:Si7050-A20", "Si7050",
                         "Package_DFN_QFN:DFN-6-1EP_3x3mm_P1mm_EP1.5x2.4mm", "SENSOR"))
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric", "SENSOR"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.add(Component("#FLG01", "power:PWR_FLAG", "PWR_FLAG"))
    ir.add(Component("#FLG02", "power:PWR_FLAG", "PWR_FLAG"))
    ir.connect("+3V3", ("U1", "5"), ("U2", "5"), ("R1", "1"), ("R2", "1"),
               ("#PWR01", "1"), ("#FLG01", "1"))
    ir.connect("GND", ("U1", "2"), ("U2", "2"), ("#PWR02", "1"), ("#FLG02", "1"))
    ir.connect("SDA", ("U1", "1"), ("U2", "1"), ("R1", "2"))
    ir.connect("SCL", ("U1", "6"), ("U2", "6"), ("R2", "2"))
    ir.nc_pins = [("U1", "3"), ("U1", "4"), ("U2", "3"), ("U2", "4")]
    return ir


def test_three_node_buses_route_as_junctioned_trees_and_roundtrip():
    ir = _board()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()}))
    placements = heuristic_place(ir, symbols)
    plan = build_emit_plan(ir, symbols, normalize_placements(ir, symbols, placements))

    tree_wires = [w for w in plan.wires if ".t" in w[2]]
    assert tree_wires, "3-node signal nets should route via the tree router"
    assert plan.junctions, "a 3-terminal tree must carry at least one junction"
    # This fixture has no explicit input connector/source. Do not move flags
    # into a dense IC field just to make a visual rail.
    assert plan.net_routes["+3V3"] == "stubs"
    assert plan.net_routes["GND"] == "stubs"

    OUT.mkdir(parents=True, exist_ok=True)
    text = emit_schematic(ir, symbols, placements)
    sch = OUT / "tree_t.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    write_project(sch)

    erc = run_erc(sch)
    assert erc.ok, [v.get("type") for v in erc.violations]

    net = OUT / "tree_t.net"
    assert export_netlist(sch, net).returncode == 0
    ok, msg = compare_connectivity(ir, net)
    assert ok, msg
