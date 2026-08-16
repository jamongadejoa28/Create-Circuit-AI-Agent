"""Typed design-rule graph selection, lowering, and topology constraints."""

from circuitgen.ir import CircuitIR, Component
from circuitgen.patterns import PatternBinding, instantiate_pattern
from circuitgen.rulegraph import (
    load_rules,
    lower_to_pattern,
    match_rules,
    validate_rule,
    verify_rule_instance,
)


def ldo_spec(*, typed: bool = True) -> dict:
    kinds = (
        ["voltage_regulator", "input_bypass_capacitor", "output_bypass_capacitor"]
        if typed else [None, None, None]
    )
    return {
        "summary": "arbitrary wording that contains no rule trigger phrase",
        "power": {"rails": [
            {"name": "+12V", "voltage": "12V"},
            {"name": "+5V", "voltage": "5V"},
            {"name": "GND", "voltage": "0V"},
        ]},
        "parts_needed": [
            {"role": "a", "search_query": "x", "functional_kind": kinds[0]},
            {"role": "b", "search_query": "x", "functional_kind": kinds[1]},
            {"role": "c", "search_query": "x", "functional_kind": kinds[2]},
        ],
    }


def test_rule_library_is_cited_and_lowers_to_binding_graph():
    rule = load_rules()["ldo_linear_regulator"]
    assert validate_rule(rule) == []
    pattern = lower_to_pattern(rule)
    assert pattern["rail_ports"] == {"VIN": "highest_supply", "VOUT": "lowest_supply"}
    assert ["VIN", "REG.IN"] in pattern["topology"]
    assert pattern["source"]["provenance"] == "textbook"
    assert pattern["roles"]["CIN"]["default_value"] == "100nF"


def test_rule_selection_uses_types_and_voltage_facts_not_prompt_words():
    rules = load_rules()
    assert [r["id"] for r in match_rules(ldo_spec(), rules)] == ["ldo_linear_regulator"]
    assert match_rules(ldo_spec(typed=False), rules) == []
    same_voltage = ldo_spec()
    same_voltage["power"]["rails"][1] = {"name": "+12V_AUX", "voltage": "12V"}
    assert match_rules(same_voltage, rules) == []


def test_draft_rules_do_not_enter_product_synthesis():
    product = load_rules()
    research = load_rules(include_unverified=True)
    assert set(product) == {"ldo_linear_regulator"}
    assert {"i2c_bus_pullup", "usb_c_sink_cc"} <= set(research)


def test_rule_verifier_rejects_input_output_short():
    rule = load_rules()["ldo_linear_regulator"]
    pattern = lower_to_pattern(rule)
    binding = PatternBinding(
        lib_ids={"REG": "Regulator_Linear:AMS1117-3.3", "CIN": "Device:C", "COUT": "Device:C"},
        pins={"REG": {"IN": "3", "OUT": "2", "GND": "1"}, "CIN": {"1": "1", "2": "2"}, "COUT": {"1": "1", "2": "2"}},
    )
    refs = {"REG": "U1", "CIN": "C1", "COUT": "C2"}
    ports = {"VIN": "+12V", "VOUT": "+5V"}
    ir = CircuitIR("ldo")
    instantiate_pattern(ir, pattern, binding, refs, ports)
    assert verify_rule_instance(ir, rule, pattern, binding, refs, ports) == []

    # Corrupt the graph after compilation: move OUT onto VIN. Required edges
    # and the explicit forbidden relation must both make the failure visible.
    for net in ir.nets:
        net.nodes = [node for node in net.nodes if node != ("U1", "2")]
    ir.connect("+12V", ("U1", "2"))
    issues = verify_rule_instance(ir, rule, pattern, binding, refs, ports)
    assert any("REG.OUT <-> VOUT broken" in issue for issue in issues)
    assert any("forbidden same net REG.IN = REG.OUT" in issue for issue in issues)
