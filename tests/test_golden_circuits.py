"""Plan §12 completion gate: all five golden circuits (§10) must pass the
full deterministic pipeline — self-ERC, KiCad ERC 0, SVG render, and
netlist connectivity round-trip. Golden 1 is covered by test_pipeline.py;
2-5 live in tests.fixtures.goldens."""

from pathlib import Path

import pytest

from tests.fixtures.goldens import (
    golden2_mcu_minimal_ir,
    golden3_mcu_i2c_ir,
    golden4_mcu_spi_ir,
    golden5_mcu_uart_ir,
)
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.partindex import DEFAULT_DB, PartIndex
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists() and DEFAULT_DB.exists()),
    reason="kicad-cli.exe / libraries / part index not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "goldens"

GOLDENS = [
    ("golden2", golden2_mcu_minimal_ir),
    ("golden3", golden3_mcu_i2c_ir),
    ("golden4", golden4_mcu_spi_ir),
    ("golden5", golden5_mcu_uart_ir),
]


@pytest.mark.parametrize("name,builder", GOLDENS, ids=[g[0] for g in GOLDENS])
def test_golden_circuit_clean(name, builder):
    ir = builder()
    res = generate(ir, OUT / name, parts_index=PartIndex())
    assert res.errors == [], res.errors
    assert res.kicad_erc is not None and res.kicad_erc.ok, [
        v.get("type") for v in res.kicad_erc.violations
    ]
    assert res.connectivity_ok, res.connectivity_msg
    assert res.svg_ok
    # §12: no nonexistent parts/pins can survive — structural self-ERC clean
    assert not [i for i in res.self_erc if i.rule.startswith("unknown")]


def test_golden3_exercises_i2c_lint_when_pullups_removed():
    ir = golden3_mcu_i2c_ir()
    for ref in ("R1", "R2"):
        ir.components.pop(ref)
    for net in ir.nets:
        net.nodes = [n for n in net.nodes if n[0] not in ("R1", "R2")]
    from circuitgen.erc import check_circuit
    from circuitgen.symbols import load_symbols

    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    issues = check_circuit(ir, symbols)
    assert any(i.rule == "i2c_pullup_missing" for i in issues)
