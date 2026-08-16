from circuitgen.geometry import Placement
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.visual import check_layout
from circuitgen.visual import check_routing
from circuitgen.emit import EmitPlan


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


def test_visual_qa_detects_different_labels_on_same_stub_endpoint():
    ir = CircuitIR("labels")
    ir.add(Component("U1", "Test:Box", "A"))
    ir.add(Component("U2", "Test:Box", "B"))
    ir.connect("NET_A", ("U1", "2"))
    ir.connect("NET_B", ("U2", "1"))
    symbols = {"Test:Box": _symbol()}
    # U1 right stub: 50.8 + 5.08 + 7.62 = 63.5.
    # U2 left stub: 76.2 - 5.08 - 7.62 = 63.5.
    issues = check_layout(
        ir, symbols,
        {"U1": {1: Placement(50.8, 50.8)}, "U2": {1: Placement(76.2, 50.8)}},
    )
    assert any(i.rule == "label_collision" for i in issues)


def test_visual_qa_detects_excessive_aspect_ratio_for_large_sheet():
    ir = CircuitIR("strip")
    symbols = {"Test:Box": _symbol()}
    placements = {}
    for i in range(8):
        ref = f"U{i + 1}"
        ir.add(Component(ref, "Test:Box", ref))
        placements[ref] = {1: Placement(30.0, 25.0 + i * 30.0)}
    issues = check_layout(ir, symbols, placements)
    assert any(i.rule == "excessive_aspect_ratio" for i in issues)


def test_visual_qa_detects_wire_touching_a_foreign_pin():
    ir = CircuitIR("foreign_pin")
    ir.add(Component("U1", "Test:Box", "A"))
    ir.add(Component("U2", "Test:Box", "B"))
    ir.connect("A", ("U1", "2"))
    ir.connect("B", ("U2", "1"))
    symbols = {"Test:Box": _symbol()}
    placements = {
        "U1": {1: Placement(50.8, 50.8)},
        "U2": {1: Placement(76.2, 50.8)},
    }
    # U2.1 is at x=71.12; a wire belonging to A crosses that foreign pin.
    plan = EmitPlan(wires=[((55.88, 50.8), (76.2, 50.8), "U1.2")])
    issues = check_routing(ir, symbols, placements, plan)
    assert any(i.rule == "wire_touches_foreign_pin" for i in issues)


def test_visual_qa_detects_different_net_wire_crossing_without_a_pin():
    ir = CircuitIR("wire_crossing")
    ir.add(Component("U1", "Test:Box", "A"))
    ir.add(Component("U2", "Test:Box", "B"))
    ir.connect("A", ("U1", "2"))
    ir.connect("B", ("U2", "1"))
    symbols = {"Test:Box": _symbol()}
    placements = {
        "U1": {1: Placement(50.8, 50.8)},
        "U2": {1: Placement(101.6, 76.2)},
    }
    plan = EmitPlan(wires=[
        ((55.88, 63.5), (81.28, 63.5), "U1.2"),
        ((68.58, 50.8), (68.58, 76.2), "U2.1"),
    ])
    issues = check_routing(ir, symbols, placements, plan)
    assert any(i.rule == "wire_crosses_foreign_wire" for i in issues)
