from circuitgen.fp_checks import check_footprints
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


class _Footprints:
    def __init__(self, pads):
        self.pads = pads

    def has_footprints(self):
        return True

    def footprint_pads(self, _fp):
        return set(self.pads)


def _symbol(numbers):
    return SymbolDef(
        "Test:Part", "(symbol \"Part\")",
        [PinDef(n, n, PinType.PASSIVE, 0, 0, 0, 2.54) for n in numbers],
    )


def test_bundled_symbol_pin_expands_to_physical_footprint_pads():
    ir = CircuitIR("bundle")
    ir.add(Component("U1", "Test:Part", "module", "Test:Module"))
    symbols = {"Test:Part": _symbol(["2", "[1,15,38,39]"])}

    assert check_footprints(
        ir, symbols, _Footprints({"1", "2", "15", "38", "39"})
    ) == []


def test_numbered_footprint_pad_without_symbol_pin_requires_review():
    ir = CircuitIR("extra_pad")
    ir.add(Component("J1", "Test:Part", "USB-C", "Test:USB-C"))
    symbols = {"Test:Part": _symbol(["A4", "B9"])}

    issues = check_footprints(
        ir, symbols, _Footprints({"A4", "A9", "B4", "B9"})
    )

    assert [(issue.rule, issue.severity) for issue in issues] == [
        ("footprint_pad_unbound", "warning")
    ]
    assert "A9" in issues[0].message and "B4" in issues[0].message
