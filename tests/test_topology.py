from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.topology import analyze_topology


def _sym(lib, prefix, pins):
    return SymbolDef(lib, "", [PinDef(n, name, et, 0, 0, 0, 2.54) for n, name, et in pins], reference_prefix=prefix)


def test_opamp_feedback_is_found_through_resistor_without_device_name():
    symbols = {
        "Amplifier_Operational:Generic": _sym("Amplifier_Operational:Generic", "U", [
            ("1", "+", PinType.INPUT), ("2", "-", PinType.INPUT), ("3", "OUT", PinType.OUTPUT)]),
        "Device:R": _sym("Device:R", "R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("amp")
    ir.add(Component("U1", "Amplifier_Operational:Generic", "OPAMP"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("OUT", ("U1", "3"), ("R1", "1"))
    ir.connect("FB", ("R1", "2"), ("U1", "2"))
    ir.connect("IN", ("U1", "1"))
    report = analyze_topology(ir, symbols)
    assert (report.amplifier_total, report.amplifier_with_feedback) == (1, 1)


def test_regulator_requires_both_input_and_output_caps():
    symbols = {
        "Regulator_Linear:Generic": _sym("Regulator_Linear:Generic", "U", [
            ("1", "IN", PinType.PWRIN), ("2", "GND", PinType.PWRIN), ("3", "OUT", PinType.PWROUT)]),
        "Device:C": _sym("Device:C", "C", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("reg")
    ir.add(Component("U1", "Regulator_Linear:Generic", "REG"))
    ir.add(Component("C1", "Device:C", "1uF"))
    ir.connect("VIN", ("U1", "1"), ("C1", "1"))
    ir.connect("GND", ("U1", "2"), ("C1", "2"))
    ir.connect("VOUT", ("U1", "3"))
    report = analyze_topology(ir, symbols)
    assert (report.regulator_total, report.regulator_with_bypass) == (1, 0)
    ir.add(Component("C2", "Device:C", "1uF"))
    ir.connect("VOUT", ("C2", "1"))
    ir.connect("GND", ("C2", "2"))
    assert analyze_topology(ir, symbols).regulator_with_bypass == 1


def test_unnamed_unique_output_pin_is_valid_for_official_opamp_symbols():
    symbols = {
        "Amplifier_Operational:BlankOut": _sym("Amplifier_Operational:BlankOut", "U", [
            ("1", "", PinType.OUTPUT), ("2", "+", PinType.INPUT), ("3", "-", PinType.INPUT)]),
    }
    ir = CircuitIR("follower")
    ir.add(Component("U1", "Amplifier_Operational:BlankOut", "OPAMP"))
    ir.connect("FOLLOW", ("U1", "1"), ("U1", "3"))
    ir.connect("IN", ("U1", "2"))
    assert analyze_topology(ir, symbols).amplifier_with_feedback == 1


def _pin(n, name, et, unit=1):
    return PinDef(n, name, et, 0, 0, 0, 2.54, unit=unit)


def test_multi_unit_opamp_units_are_analyzed_independently():
    """LM358-shape: two amp units + power unit; only wired units count."""
    lm = SymbolDef("Amplifier_Operational:LM358", "", [
        _pin("1", "", PinType.OUTPUT, 1), _pin("2", "-", PinType.INPUT, 1), _pin("3", "+", PinType.INPUT, 1),
        _pin("5", "+", PinType.INPUT, 2), _pin("6", "-", PinType.INPUT, 2), _pin("7", "", PinType.OUTPUT, 2),
        _pin("4", "V-", PinType.PWRIN, 3), _pin("8", "V+", PinType.PWRIN, 3),
    ], reference_prefix="U")
    symbols = {
        "Amplifier_Operational:LM358": lm,
        "Device:R": _sym("Device:R", "R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("dual")
    ir.add(Component("U1", "Amplifier_Operational:LM358", "LM358"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("OUT", ("U1", "1"), ("R1", "1"))
    ir.connect("INV", ("U1", "2"), ("R1", "2"))
    report = analyze_topology(ir, symbols)
    # unit 1 wired with feedback; unit 2 completely unused -> not counted
    assert (report.amplifier_total, report.amplifier_with_feedback) == (1, 1)


def test_feedback_path_may_not_transit_ground():
    """Load R to GND plus bias R from GND is open-loop, not feedback."""
    symbols = {
        "Amplifier_Operational:Generic": _sym("Amplifier_Operational:Generic", "U", [
            ("1", "+", PinType.INPUT), ("2", "-", PinType.INPUT), ("3", "OUT", PinType.OUTPUT)]),
        "Device:R": _sym("Device:R", "R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
        "power:GND": SymbolDef("power:GND", "", [_pin("1", "GND", PinType.PWRIN)], is_power=True),
    }
    ir = CircuitIR("openloop")
    ir.add(Component("U1", "Amplifier_Operational:Generic", "OPAMP"))
    ir.add(Component("R1", "Device:R", "1k"))
    ir.add(Component("R2", "Device:R", "1k"))
    ir.add(Component("#PWR01", "power:GND", "GND"))
    ir.connect("OUT", ("U1", "3"), ("R1", "1"))
    ir.connect("GND", ("R1", "2"), ("R2", "2"), ("#PWR01", "1"))
    ir.connect("INV", ("U1", "2"), ("R2", "1"))
    report = analyze_topology(ir, symbols)
    assert (report.amplifier_total, report.amplifier_with_feedback) == (1, 0)


def test_bleed_resistor_does_not_mask_real_bypass_cap():
    symbols = {
        "Regulator_Linear:Generic": _sym("Regulator_Linear:Generic", "U", [
            ("1", "IN", PinType.PWRIN), ("2", "GND", PinType.PWRIN), ("3", "OUT", PinType.PWROUT)]),
        "Device:C": _sym("Device:C", "C", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
        "Device:R": _sym("Device:R", "R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("bleed")
    ir.add(Component("U1", "Regulator_Linear:Generic", "REG"))
    ir.add(Component("R1", "Device:R", "10k"))   # bleed parallel to C2
    ir.add(Component("C1", "Device:C", "10uF"))
    ir.add(Component("C2", "Device:C", "22uF"))
    ir.connect("VIN", ("U1", "1"), ("C1", "1"))
    ir.connect("VOUT", ("U1", "3"), ("R1", "1"), ("C2", "1"))
    ir.connect("GND", ("U1", "2"), ("C1", "2"), ("R1", "2"), ("C2", "2"))
    report = analyze_topology(ir, symbols)
    assert (report.regulator_total, report.regulator_with_bypass) == (1, 1)
