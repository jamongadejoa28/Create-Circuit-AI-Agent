"""Phase 3 exit criterion: an MCU circuit through the deterministic
pipeline alone (no LLM) with heuristic placement — KiCad ERC 0.

ESP32-WROOM-32 minimal circuit in the golden-circuit-2/3 mold: decoupling
on VDD, EN (reset) pull-up, IO0 boot strap pull-up, I2C bus with pull-ups,
every unused IO explicitly no-connected. Values come from the knowledge
base (decoupling-cap-per-ic: 0.1uF; pullup-resistor-sizing: 10k).
"""

from pathlib import Path

import pytest

from circuitgen.erc import check_circuit
from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated"

MCU = "RF_Module:ESP32-WROOM-32"


def esp32_minimal_ir() -> CircuitIR:
    ir = CircuitIR(name="esp32_minimal")
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.add(Component("U1", MCU, "ESP32-WROOM-32", "RF_Module:ESP32-WROOM-32"))
    ir.add(Component("C1", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # EN pull-up
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # IO0 boot strap
    ir.add(Component("R3", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # SDA pull-up
    ir.add(Component("R4", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # SCL pull-up
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    ir.connect(
        "+3V3",
        ("U1", "2"),  # VDD
        ("C1", "1"),
        ("R1", "1"),
        ("R2", "1"),
        ("R3", "1"),
        ("R4", "1"),
        ("#PWR01", "1"),
    )
    # all GND pins including the hidden stacked ones (15/38/39)
    ir.connect("GND", ("U1", "1"), ("U1", "15"), ("U1", "38"), ("U1", "39"),
               ("C1", "2"), ("#PWR02", "1"))
    ir.connect("EN", ("U1", "3"), ("R1", "2"))
    ir.connect("IO0", ("U1", "25"), ("R2", "2"))
    ir.connect("SDA", ("U1", "33"), ("R3", "2"))  # IO21
    ir.connect("SCL", ("U1", "36"), ("R4", "2"))  # IO22

    # every remaining pin is an explicit no-connect (pin 32 is NC-typed
    # in the library and needs no marker)
    used = {"1", "2", "3", "15", "25", "32", "33", "36", "38", "39"}
    ir.nc_pins = [("U1", str(n)) for n in range(1, 40) if str(n) not in used]
    return ir


def test_esp32_minimal_pipeline_clean():
    res = generate(esp32_minimal_ir(), OUT / "esp32")
    assert res.errors == [], res.errors
    assert res.kicad_erc.ok, [
        (v.get("type"), i.get("description"))
        for v in res.kicad_erc.violations
        for i in v.get("items", [{}])
    ]
    assert res.connectivity_ok, res.connectivity_msg
    assert res.svg_ok
    # the design follows the knowledge rules, so the §8.2 lint stays quiet
    assert not [i for i in res.self_erc if i.rule in ("decoupling_missing", "i2c_pullup_missing")]


def test_esp32_decoupling_cap_placed_beside_mcu():
    from circuitgen.place import heuristic_place

    ir = esp32_minimal_ir()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    ensure_pwr_flags(ir, symbols)
    placements = heuristic_place(ir, symbols)
    mcu = placements["U1"][1]
    cap = placements["C1"][1]
    # shelf-packed group tiles place the cap in the MCU's tile row; the
    # adjacency budget widened accordingly (was satellite-column 45/30)
    assert abs(cap.x - mcu.x) < 85 and abs(cap.y - mcu.y) < 55, (
        f"decoupling cap at {cap} not adjacent to MCU at {mcu}"
    )


def test_i2c_rule_fires_on_named_nets_without_pullups():
    ir = esp32_minimal_ir()
    # sabotage: remove the SDA pull-up
    for net in ir.nets:
        if net.name == "SDA":
            net.nodes = [n for n in net.nodes if n[0] != "R3"]
    ir.components.pop("R3")
    ir.nets = [n for n in ir.nets if n.name != "+3V3" or True]
    for net in ir.nets:
        if net.name == "+3V3":
            net.nodes = [n for n in net.nodes if n[0] != "R3"]
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    ensure_pwr_flags(ir, symbols)
    issues = check_circuit(ir, symbols)
    assert any(i.rule == "i2c_pullup_missing" and "SDA" in i.path for i in issues)
