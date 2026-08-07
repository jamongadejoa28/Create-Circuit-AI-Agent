from circuitgen.geometry import Placement
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.visual import check_layout


def _symbol():
    return SymbolDef(
        "Test:Box", "(symbol \"Box\")",
        [PinDef("1", "IN", PinType.INPUT, -5.08, 0, 0, 2.54),
         PinDef("2", "OUT", PinType.OUTPUT, 5.08, 0, 180, 2.54)],
    )


def test_visual_qa_detects_symbol_overlap():
    ir = CircuitIR("visual")
    ir.add(Component("U1", "Test:Box", "A"))
    ir.add(Component("U2", "Test:Box", "B"))
    symbols = {"Test:Box": _symbol()}
    issues = check_layout(
        ir, symbols,
        {"U1": {1: Placement(50.8, 50.8)}, "U2": {1: Placement(55.88, 50.8)}},
    )
    assert any(i.rule == "symbol_overlap" for i in issues)


def test_visual_qa_accepts_spaced_symbols():
    ir = CircuitIR("visual")
    ir.add(Component("U1", "Test:Box", "A"))
    ir.add(Component("U2", "Test:Box", "B"))
    symbols = {"Test:Box": _symbol()}
    assert check_layout(
        ir, symbols,
        {"U1": {1: Placement(50.8, 50.8)}, "U2": {1: Placement(101.6, 50.8)}},
    ) == []
