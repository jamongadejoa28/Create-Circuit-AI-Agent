"""Netlist generation / partition logic."""

from simp_sexp import Sexp

from circuitgen.examples import golden_led_button_ir
from circuitgen.ir import CircuitIR, Component
from circuitgen.netlist import generate_netlist, ir_partition
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

import pytest

pytestmark = pytest.mark.skipif(
    not KICAD_SYMBOL_DIR.exists(), reason="KiCad bundled libraries not mounted"
)


def test_partition_excludes_power_symbols_and_singletons():
    ir = golden_led_button_ir()
    part = ir_partition(ir)
    # +5V and GND collapse to single real nodes → excluded; two signal nets remain
    assert part == {
        frozenset({("SW1", "2"), ("R1", "1")}),
        frozenset({("R1", "2"), ("D1", "2")}),
    }


def test_generated_netlist_parses_and_lists_all_nodes():
    ir = golden_led_button_ir()
    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}))
    ensure_pwr_flags(ir, symbols)
    text = generate_netlist(ir, symbols)
    sx = Sexp(text)
    nets = sx.search("/export/nets/net")
    assert len(nets) == 4
    comps = sx.search("/export/components/comp")
    assert len(comps) == len(ir.components)
