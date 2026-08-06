"""Symbol library parser against the real KiCad 10 bundled libraries.

Expected pin data comes from independently verified ground truth
(pin tables extracted by direct file inspection of Device/Switch/power
.kicad_sym, cross-checked against the same files by a separate pass).
"""

import pytest

from circuitgen.pins import PinType
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols, parse_library

pytestmark = pytest.mark.skipif(
    not KICAD_SYMBOL_DIR.exists(), reason="KiCad bundled libraries not mounted"
)


def test_basic_pin_tables():
    syms = load_symbols(["Device:R", "Device:LED", "Switch:SW_Push"])
    r = syms["Device:R"]
    assert [(p.number, p.etype, p.x, p.y, p.orientation) for p in r.pins] == [
        ("1", PinType.PASSIVE, 0.0, 3.81, 270),
        ("2", PinType.PASSIVE, 0.0, -3.81, 90),
    ]
    assert r.reference_prefix == "R" and not r.is_power

    led = syms["Device:LED"]
    assert [(p.number, p.name) for p in led.pins] == [("1", "K"), ("2", "A")]

    sw = syms["Switch:SW_Push"]
    assert [(p.number, p.x) for p in sw.pins] == [("1", -5.08), ("2", 5.08)]
    assert sw.reference_prefix == "SW"


def test_power_symbols():
    syms = load_symbols(["power:+5V", "power:GND", "power:PWR_FLAG"])
    assert all(s.is_power for s in syms.values())
    assert syms["power:+5V"].pins[0].etype == PinType.PWRIN
    assert syms["power:GND"].pins[0].etype == PinType.PWRIN
    assert syms["power:PWR_FLAG"].pins[0].etype == PinType.PWROUT
    assert syms["power:PWR_FLAG"].reference_prefix == "#FLG"


def test_raw_blocks_balanced_and_named():
    syms = load_symbols(["Device:R", "power:GND"])
    for s in syms.values():
        assert s.raw_sexp.count("(") == s.raw_sexp.count(")")
        name = s.lib_id.split(":")[1]
        assert s.raw_sexp.lstrip().startswith(f'(symbol "{name}"')


def test_extends_inheritance_resolved():
    # Switch.kicad_sym contains derived symbols using (extends ...).
    defs = parse_library(KICAD_SYMBOL_DIR / "Switch.kicad_sym", "Switch")
    derived = {
        name: d
        for name, d in defs.items()
        if "(extends" in d.raw_sexp
    }
    assert derived, "expected at least one extends-based symbol in Switch"
    for d in derived.values():
        assert d.pins, f"{d.lib_id}: derived symbol must inherit parent pins"
