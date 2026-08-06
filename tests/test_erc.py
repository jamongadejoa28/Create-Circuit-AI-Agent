"""Unit tests for the self-hosted ERC (SKIDL-ported rules)."""

from circuitgen.erc import check_circuit
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.pins import PinType


def sym(lib_id, pin_specs, is_power=False):
    return SymbolDef(
        lib_id=lib_id,
        raw_sexp=f'(symbol "{lib_id.split(":")[1]}")',
        pins=[
            PinDef(number=n, name=n, etype=t, x=0, y=0, orientation=0, length=1.27)
            for n, t in pin_specs
        ],
        is_power=is_power,
    )


SYMS = {
    "test:OUT2": sym("test:OUT2", [("1", PinType.OUTPUT), ("2", PinType.PASSIVE)]),
    "test:R": sym("test:R", [("1", PinType.PASSIVE), ("2", PinType.PASSIVE)]),
    "test:NC1": sym("test:NC1", [("1", PinType.NOCONNECT)]),
    "test:IN1": sym("test:IN1", [("1", PinType.INPUT)]),
    "power:VCC": sym("power:VCC", [("1", PinType.PWRIN)], is_power=True),
    "power:PWR_FLAG": sym("power:PWR_FLAG", [("1", PinType.PWROUT)], is_power=True),
}


def rules(issues):
    return {(i.rule, i.severity) for i in issues}


def test_clean_two_passive_net():
    ir = CircuitIR("t")
    ir.add(Component("R1", "test:R", "1k"))
    ir.add(Component("R2", "test:R", "1k"))
    ir.connect("A", ("R1", "1"), ("R2", "1"))
    ir.connect("B", ("R1", "2"), ("R2", "2"))
    issues = check_circuit(ir, SYMS)
    # PASSIVE drive exceeds NONE, so even a passive-only net has "a driver"
    # in SKiDL's model — a clean R-R circuit reports nothing at all.
    assert issues == []


def test_output_output_conflict_is_error():
    ir = CircuitIR("t")
    ir.add(Component("U1", "test:OUT2", "x"))
    ir.add(Component("U2", "test:OUT2", "x"))
    ir.connect("N", ("U1", "1"), ("U2", "1"))
    ir.connect("M", ("U1", "2"), ("U2", "2"))
    issues = check_circuit(ir, SYMS)
    assert ("pin_conflict", "error") in rules(issues)


def test_unconnected_pin_warned_and_nc_marker_accepted():
    ir = CircuitIR("t")
    ir.add(Component("R1", "test:R", "1k"))
    ir.add(Component("R2", "test:R", "1k"))
    ir.connect("A", ("R1", "1"), ("R2", "1"))
    issues = check_circuit(ir, SYMS)
    unconnected = [i for i in issues if i.rule == "unconnected_pin"]
    assert {i.path for i in unconnected} == {"R1.2", "R2.2"}

    ir.nc_pins = [("R1", "2"), ("R2", "2")]
    issues = check_circuit(ir, SYMS)
    assert not [i for i in issues if i.rule == "unconnected_pin"]


def test_nc_typed_pin_connected_is_error():
    ir = CircuitIR("t")
    ir.add(Component("U1", "test:NC1", "x"))
    ir.add(Component("R1", "test:R", "1k"))
    ir.connect("N", ("U1", "1"), ("R1", "1"))
    issues = check_circuit(ir, SYMS)
    assert ("nc_pin_connected", "error") in rules(issues)
    # conflict matrix also fires: NOCONNECT × PASSIVE = ERROR
    assert ("pin_conflict", "error") in rules(issues)


def test_unknown_symbol_pin_and_duplicate_membership():
    ir = CircuitIR("t")
    ir.add(Component("U9", "test:MISSING", "x"))
    ir.add(Component("R1", "test:R", "1k"))
    ir.connect("N", ("U9", "1"), ("R1", "7"), ("R1", "1"))
    ir.connect("M", ("R1", "1"), ("X1", "1"))
    got = rules(check_circuit(ir, SYMS))
    assert ("unknown_symbol", "error") in got
    assert ("unknown_pin", "error") in got
    assert ("unknown_component", "error") in got
    assert ("pin_multiple_nets", "error") in got


def test_power_net_needs_pwr_flag_for_drive():
    ir = CircuitIR("t")
    ir.add(Component("U1", "test:IN1", "x"))
    ir.add(Component("#PWR01", "power:VCC", "VCC"))
    ir.connect("VCC", ("U1", "1"), ("#PWR01", "1"))
    issues = check_circuit(ir, SYMS)
    # PWRIN needs POWER drive; without PWR_FLAG the net max drive is NONE
    assert ("insufficient_drive", "warning") in rules(issues)

    added = ensure_pwr_flags(ir, SYMS)
    assert added == ["#FLG01"]
    issues = check_circuit(ir, SYMS)
    assert ("insufficient_drive", "warning") not in rules(issues)


def test_pwr_flag_not_duplicated():
    ir = CircuitIR("t")
    ir.add(Component("U1", "test:IN1", "x"))
    ir.add(Component("#PWR01", "power:VCC", "VCC"))
    ir.connect("VCC", ("U1", "1"), ("#PWR01", "1"))
    assert ensure_pwr_flags(ir, SYMS) == ["#FLG01"]
    assert ensure_pwr_flags(ir, SYMS) == []  # second run adds nothing
