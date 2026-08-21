"""Netlist generation / partition logic."""

from simp_sexp import Sexp

from circuitgen.ir import CircuitIR, Component
from circuitgen.netlist import compare_connectivity, generate_netlist, ir_partition
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols
from tests.fixtures.circuits import led_button_ir

import pytest

pytestmark = pytest.mark.skipif(
    not KICAD_SYMBOL_DIR.exists(), reason="KiCad bundled libraries not mounted"
)


def test_partition_excludes_power_symbols_and_singletons():
    ir = led_button_ir()
    part = ir_partition(ir)
    # +5V and GND collapse to single real nodes → excluded; two signal nets remain
    assert part == {
        frozenset({("SW1", "2"), ("R1", "1")}),
        frozenset({("R1", "2"), ("D1", "2")}),
    }


def test_generated_netlist_parses_and_lists_all_nodes():
    ir = led_button_ir()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    ensure_pwr_flags(ir, symbols)
    text = generate_netlist(ir, symbols)
    sx = Sexp(text)
    nets = sx.search("/export/nets/net")
    assert len(nets) == 4
    comps = sx.search("/export/components/comp")
    assert len(comps) == len(ir.components)


def test_round_trip_decodes_sheet_prefix_and_escaped_slashes(tmp_path):
    """KiCad qualifies local labels and escapes a slash that is label text."""
    ir = CircuitIR("escaped_names")
    ir.add(Component("R1", "Device:R", "1k"))
    ir.add(Component("R2", "Device:R", "1k"))
    ir.connect("/MCU/SIGNAL", ("R1", "1"), ("R2", "1"))
    ir.connect("DATA[0]", ("R1", "2"), ("R2", "2"))
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()}))
    exported = generate_netlist(ir, symbols)
    exported = exported.replace(
        '(name "/MCU/SIGNAL")', '(name "/{slash}MCU{slash}SIGNAL")'
    ).replace('(name "DATA[0]")', '(name "/DATA[0]")')
    path = tmp_path / "escaped.net"
    path.write_text(exported, encoding="utf-8")

    ok, message = compare_connectivity(ir, path)

    assert ok, message
