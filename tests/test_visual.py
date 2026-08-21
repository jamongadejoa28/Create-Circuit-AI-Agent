"""Semantic coordinate QA only; these tests do not inspect rendered pixels."""

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


def test_visual_qa_ignores_orthogonal_mid_segment_crossing():
    """KiCad does not join crossed wires without a shared vertex/junction."""
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
    assert not [
        i for i in issues if i.rule == "wire_crosses_foreign_wire"
    ]


def test_visual_qa_detects_t_junction_onto_foreign_wire():
    ir = CircuitIR("wire_t_junction")
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
        # Vertical wire ends on the horizontal mid-segment → KiCad joins.
        ((68.58, 50.8), (68.58, 63.5), "U2.1"),
    ])
    issues = check_routing(ir, symbols, placements, plan)
    assert any(i.rule == "wire_crosses_foreign_wire" for i in issues)
