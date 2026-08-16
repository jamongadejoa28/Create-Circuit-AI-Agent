from circuitgen.interfaces import analyze_interfaces, interface_metrics
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def test_interfaces_derive_types_drivers_and_group_crossings_from_graph():
    symbols = {
        "X:Source": SymbolDef("X:Source", "", [
            PinDef("1", "OUT", PinType.OUTPUT, 0, 0, 0, 2.54),
            PinDef("2", "VCC", PinType.PWRIN, 0, 2.54, 0, 2.54),
        ]),
        "X:Sink": SymbolDef("X:Sink", "", [
            PinDef("1", "IN", PinType.INPUT, 0, 0, 180, 2.54),
            PinDef("2", "VCC", PinType.PWRIN, 0, 2.54, 180, 2.54),
        ]),
    }
    ir = CircuitIR("typed")
    ir.add(Component("U1", "X:Source", "source", group="source"))
    ir.add(Component("U2", "X:Sink", "sink", group="sink"))
    ir.connect("DATA", ("U1", "1"), ("U2", "1"))
    ir.connect("VCC", ("U1", "2"), ("U2", "2"))

    interfaces = analyze_interfaces(ir, symbols)
    assert interfaces["DATA"].kind == "signal"
    assert interfaces["DATA"].drivers == frozenset({"U1"})
    assert interfaces["DATA"].consumers == frozenset({"U2"})
    assert interfaces["DATA"].groups == frozenset({"source", "sink"})
    assert interfaces["VCC"].kind == "power"
    assert interface_metrics(ir, symbols) == {
        "typed_nets": 2,
        "by_kind": {"ground": 0, "power": 1, "signal": 1},
        "driven_signal_nets": 1,
        "cross_group_nets": 2,
    }
