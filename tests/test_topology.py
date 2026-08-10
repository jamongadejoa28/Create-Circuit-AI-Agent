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


# --- conduction: is the part wired so current can flow through it? ---------
#
# Every case below is a board this pipeline actually produced, not an invented
# shape. "Is the role present" reported 1.0 on all of them.

from circuitgen.topology import analyze_conduction


def _passive(lib, prefix):
    return _sym(lib, prefix, [("1", "", PinType.PASSIVE), ("2", "", PinType.PASSIVE)])


SYMS = {
    "Device:R": _passive("Device:R", "R"),
    "Device:C": _passive("Device:C", "C"),
    "Diode:1N4148": _sym("Diode:1N4148", "D", [
        ("1", "K", PinType.PASSIVE), ("2", "A", PinType.PASSIVE)]),
    "Transistor_BJT:BC337": _sym("Transistor_BJT:BC337", "Q", [
        ("1", "C", PinType.PASSIVE), ("2", "B", PinType.INPUT),
        ("3", "E", PinType.PASSIVE)]),
    "Connector_Generic:Conn_01x02": _passive("Connector_Generic:Conn_01x02", "J"),
    "Interface_CAN_LIN:Generic": _sym("Interface_CAN_LIN:Generic", "U", [
        ("6", "CANL", PinType.BIDIR), ("7", "CANH", PinType.BIDIR)]),
}


def test_a_resistor_whose_ends_reach_the_same_rail_carries_no_current():
    """driver_relay seed 202: a repair round invented R2..R5 and hung two of
    them off one dead net. Both ends of R2 arrive at +5V — through itself and
    through R3 — so no current flows, and ERC has nothing to say about it."""
    ir = CircuitIR("dead")
    ir.add(Component("R2", "Device:R", "1k"))
    ir.add(Component("R3", "Device:R", "1k"))
    ir.add(Component("PW", "power:+5V", "+5V"))
    SYMS["power:+5V"] = _sym("power:+5V", "#PWR", [("1", "+5V", PinType.PWROUT)])
    SYMS["power:+5V"].is_power = True
    ir.connect("+5V", ("R2", "1"), ("R3", "1"), ("PW", "1"))
    ir.connect("DEAD", ("R2", "2"), ("R3", "2"))
    report = analyze_conduction(ir, SYMS)
    assert set(report.dead) == {"R2", "R3"}
    assert "same potential" in report.dead["R2"]


def test_a_pin_alone_on_its_net_means_the_part_is_not_wired_in():
    """driver_relay seed 201: the transistor collector sat on net K1_C whose
    only member was that pin. Presence said the transistor was there."""
    ir = CircuitIR("lonely")
    ir.add(Component("Q1", "Transistor_BJT:BC337", "BC337"))
    ir.add(Component("R1", "Device:R", "1k"))
    ir.connect("+5V", ("Q1", "2"), ("R1", "1"))
    ir.connect("GND", ("Q1", "3"), ("R1", "2"))
    ir.connect("K1_C", ("Q1", "1"))
    report = analyze_conduction(ir, SYMS)
    assert "Q1" in report.dead and "only thing on its net" in report.dead["Q1"]


def test_a_supply_net_without_a_power_symbol_is_still_a_rail():
    """digital_control draws +3V3 from a conceptual supply block, so the net
    holds no #PWR symbol. Without the naming half of the rail test the trace
    runs out of one decoupling capacitor straight into its neighbour and every
    cap on the board is reported as GND-to-GND."""
    ir = CircuitIR("decoupling")
    for ref in ("C1", "C2", "C3"):
        ir.add(Component(ref, "Device:C", "100nF"))
    # the supply arrives as a two-pin conceptual box, so nothing on +3V3 is a
    # power symbol and nothing on it is a multi-terminal device either
    SYMS["Conceptual:3_3V_power_supply"] = _passive(
        "Conceptual:3_3V_power_supply", "U"
    )
    ir.add(Component("PS1", "Conceptual:3_3V_power_supply", "3V3"))
    ir.connect("+3V3", ("C1", "1"), ("C2", "1"), ("C3", "1"), ("PS1", "1"))
    ir.connect("GND", ("C1", "2"), ("C2", "2"), ("C3", "2"), ("PS1", "2"))
    report = analyze_conduction(ir, SYMS)
    assert report.dead == {}, report.dead


def test_a_two_pin_connector_is_not_a_path_between_its_pins():
    """communication_can, ERC 0 and correct: CANH and CANL leave through a
    2-pin header. Treating the header as a series element merges the two bus
    lines and reports the 120 ohm termination — the one part of that board the
    user could not have placed — as carrying no current."""
    ir = CircuitIR("can")
    ir.add(Component("U2", "Interface_CAN_LIN:Generic", "TJA1051T"))
    ir.add(Component("R1", "Device:R", "120R"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x02", "Conn_01x02"))
    ir.connect("CANH", ("U2", "7"), ("R1", "1"), ("J1", "1"))
    ir.connect("CANL", ("U2", "6"), ("R1", "2"), ("J1", "2"))
    report = analyze_conduction(ir, SYMS)
    assert report.dead == {}, report.dead
