from circuitgen.contracts import contract_instructions, infer_contracts, repair_contracts, validate_contracts
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib, prefix, pins):
    return SymbolDef(lib, "", [PinDef(n, name, et, 0, 0, 0, 2.54) for n, name, et in pins], reference_prefix=prefix)


def test_contract_inference_distinguishes_opamp_and_comparator():
    opamp = {"summary": "non-inverting op-amp amplifier", "parts_needed": [], "connections_intent": []}
    comparator = {"summary": "comparator threshold detector", "parts_needed": [], "connections_intent": []}
    assert infer_contracts(opamp) == ["amplifier_feedback"]
    assert infer_contracts(comparator) == []
    assert "closed-loop" in contract_instructions(infer_contracts(opamp))[0]


def test_missing_feedback_is_a_functional_failure_not_an_erc_guess():
    lib = "Amplifier_Operational:Generic"
    symbols = {lib: _sym(lib, "U", [
        ("1", "+", PinType.INPUT), ("2", "-", PinType.INPUT),
        ("3", "", PinType.OUTPUT),
    ])}
    ir = CircuitIR("open_loop")
    ir.add(Component("U1", lib, "OPAMP"))
    ir.connect("INP", ("U1", "1"))
    ir.connect("INN", ("U1", "2"))
    ir.connect("OUT", ("U1", "3"))
    assert validate_contracts(ir, symbols, ["amplifier_feedback"]) == [
        "amplifier_feedback: 0/1 amplifier(s) have feedback"
    ]


def test_regulator_contract_is_inferred_from_role_not_part_number():
    spec = {
        "summary": "power supply",
        "parts_needed": [{"role": "main_regulator", "search_query": "linear voltage regulator"}],
        "connections_intent": [],
    }
    assert infer_contracts(spec) == ["regulator_input_output_bypass"]


def test_explicit_feedback_role_can_be_deterministically_rewired():
    amp = "Amplifier_Operational:Generic"
    resistor = "Device:R"
    symbols = {
        amp: _sym(amp, "U", [("1", "+", PinType.INPUT), ("2", "-", PinType.INPUT), ("3", "", PinType.OUTPUT)]),
        resistor: _sym(resistor, "R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("repair_feedback")
    ir.add(Component("U1", amp, "OPAMP"))
    ir.add(Component("R1", resistor, "10k ohm"))
    ir.connect("INP", ("U1", "1"), ("R1", "1"))
    ir.connect("INN", ("U1", "2"))
    ir.connect("OUT", ("U1", "3"), ("R1", "2"))
    spec = {"parts_needed": [{"role": "feedback_resistor", "value": "10k"}]}
    notes = repair_contracts(ir, symbols, spec, ["amplifier_feedback"])
    assert notes
    assert validate_contracts(ir, symbols, ["amplifier_feedback"]) == []


def test_regulator_contract_repair_corrects_swapped_semantic_pins():
    reg, cap = "Regulator_Linear:Generic", "Device:C"
    symbols = {
        reg: _sym(reg, "U", [("1", "OUT", PinType.PWROUT), ("2", "GND", PinType.PWRIN), ("3", "IN", PinType.PWRIN)]),
        cap: _sym(cap, "C", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("swapped")
    ir.add(Component("U1", reg, "REG")); ir.add(Component("C1", cap, "1uF")); ir.add(Component("C2", cap, "1uF"))
    ir.connect("+12V", ("U1", "2"), ("C1", "1"))
    ir.connect("GND", ("U1", "3"), ("C1", "2"), ("C2", "2"))
    ir.connect("+5V", ("U1", "1"), ("C2", "1"))
    spec = {"power": {"rails": [{"name": "+12V"}, {"name": "GND"}, {"name": "+5V"}]}, "parts_needed": []}
    repair_contracts(ir, symbols, spec, ["regulator_input_output_bypass"])
    node_net = {(r, p): n.name for n in ir.nets for r, p in n.nodes}
    assert node_net[("U1", "3")] == "+12V"
    assert node_net[("U1", "2")] == "GND"
    assert node_net[("U1", "1")] == "+5V"
