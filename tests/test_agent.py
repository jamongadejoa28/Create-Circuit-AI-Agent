"""Agent orchestrator against a mock LLM backend.

Everything except the model is real: part index (small fixture), knowledge
index, self-ERC, emitter, kicad-cli oracle. The mock returns canned
schema-shaped payloads, letting us test the staging, validation and
repair-loop mechanics deterministically. The real-model end-to-end run
lives in scripts/run_agent.py (needs llama-server with a loaded model).
"""

from pathlib import Path

import pytest

from circuitgen.agent import Agent
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.knowledge import KNOWLEDGE_DIR, KnowledgeIndex, build_index as build_kn
from circuitgen.ir import CircuitIR, Component
from circuitgen.partindex import LibrarySource, PartIndex, build_index as build_parts
from circuitgen.symbols import KICAD_SYMBOL_DIR

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)


class MockLLM:
    """Returns canned payloads keyed by schema shape; records prompts."""

    def __init__(self, spec=None, irs=None, patches=None):
        self.spec = spec
        self.irs = list(irs or [])
        self.patches = list(patches or [])
        self.calls: list[str] = []

    def complete_json(self, messages, schema, **kw):
        req = set(schema.get("required", []))
        if "parts_needed" in req:
            self.calls.append("spec")
            return self.spec
        if "components" in req:
            self.calls.append("ir")
            return self.irs.pop(0)
        if "ops" in req:
            self.calls.append("patch")
            return self.patches.pop(0)
        raise AssertionError(f"unexpected schema: {sorted(req)}")


def test_mcu_requirement_gets_missing_3v3_logic_rail():
    spec = {
        "power": {"rails": [{"name": "+12V", "voltage": "12V"}, {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [{"role": "controller", "search_query": "STM32G474"}],
    }
    Agent._ensure_logic_rail(spec)
    assert [r["name"] for r in spec["power"]["rails"]] == ["+12V", "GND", "+3V3"]
    Agent._ensure_logic_rail(spec)
    assert [r["name"] for r in spec["power"]["rails"]].count("+3V3") == 1


def test_explicit_input_and_output_voltage_rails_survive_extractor_omission():
    spec = {"power": {"rails": [{"name": "+12V", "voltage": "12V"}, {"name": "GND", "voltage": "0V"}]}}
    Agent._ensure_explicit_voltage_rails("12V 입력을 5V로 변환하고 3.3V를 추가", spec)
    assert [r["name"] for r in spec["power"]["rails"]] == ["+12V", "GND", "+5V", "+3V3"]


SPEC = {
    "summary": "5V push button lights an LED through a resistor",
    "power": {"rails": [{"name": "+5V", "voltage": "5V"}, {"name": "GND", "voltage": "0V"}]},
    "parts_needed": [
        {"role": "btn1", "search_query": "push button switch", "value": "SW_Push"},
        {"role": "r1", "search_query": "resistor", "value": "330R"},
        {"role": "led1", "search_query": "LED", "value": "LED"},
    ],
    "connections_intent": ["+5V - btn1 - r1 - led1 anode, cathode to GND"],
}

GOOD_IR = {
    "name": "agent_led_button",
    "components": [
        {"ref": "SW1", "lib_id": "Switch:SW_Push", "value": "SW_Push", "footprint": "Button_Switch_SMD:SW_SPST_PTS645Sx43SMTR92"},
        {"ref": "R1", "lib_id": "Device:R", "value": "330R", "footprint": "Resistor_SMD:R_0805_2012Metric"},
        {"ref": "D1", "lib_id": "Device:LED", "value": "LED", "footprint": "LED_SMD:LED_0805_2012Metric"},
        {"ref": "#PWR01", "lib_id": "power:+5V", "value": "+5V"},
        {"ref": "#PWR02", "lib_id": "power:GND", "value": "GND"},
    ],
    "nets": [
        {"name": "+5V", "nodes": [{"ref": "SW1", "pin": "1"}, {"ref": "#PWR01", "pin": "1"}]},
        {"name": "SW_R", "nodes": [{"ref": "SW1", "pin": "2"}, {"ref": "R1", "pin": "1"}]},
        {"name": "R_LED", "nodes": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "2"}]},
        {"name": "GND", "nodes": [{"ref": "D1", "pin": "1"}, {"ref": "#PWR02", "pin": "1"}]},
    ],
    "nc_pins": [],
}


@pytest.fixture(scope="module")
def agent_env(tmp_path_factory):
    import shutil

    from circuitgen.symbols import library_path

    tmp = tmp_path_factory.mktemp("agent")
    subset = tmp / "libs"
    subset.mkdir()
    for name in (
        "Device", "Switch", "power", "Amplifier_Operational",
        "Connector_Generic", "Regulator_Linear", "MCU_ST_STM32G4",
        "Sensor_Temperature", "Interface_CAN_LIN",
    ):
        src = library_path(KICAD_SYMBOL_DIR, name)
        if src.is_dir():
            shutil.copytree(src, subset / src.name)
        else:
            shutil.copy(src, subset / src.name)
    pdb = tmp / "parts.sqlite"
    build_parts(pdb, sources=[LibrarySource(subset, "", 1, "CC-BY-SA-4.0")])
    kdb = tmp / "knowledge.sqlite"
    build_kn(kdb, KNOWLEDGE_DIR)
    return PartIndex(pdb), KnowledgeIndex(kdb), tmp


def test_agent_happy_path(agent_env):
    parts, knowledge, tmp = agent_env
    llm = MockLLM(spec=SPEC, irs=[GOOD_IR])
    agent = Agent(llm, parts, knowledge, tmp / "out1")
    res = agent.run("5V에서 버튼 누르면 LED 켜지는 회로", name="agent_led_button")
    assert res.ok, (res.stage, res.log, res.pipeline.errors if res.pipeline else None)
    assert res.pipeline.kicad_erc.ok
    assert res.pipeline.connectivity_ok
    assert llm.calls == ["spec", "ir"]  # no repair rounds needed


def test_agent_repair_loop_fixes_unconnected_pin(agent_env):
    parts, knowledge, tmp = agent_env
    bad_ir = {
        **GOOD_IR,
        "name": "agent_led_button_bad",
        # R1.2 <-> D1.2 net omitted: two unconnected pins -> self-ERC errors
        "nets": [n for n in GOOD_IR["nets"] if n["name"] != "R_LED"],
    }
    patch = {
        "analysis": "R1.2 and D1.2 are unconnected; the series link is missing",
        "ops": [
            {"op": "connect", "ref": "R1", "pin": "2", "net": "R_LED"},
            {"op": "connect", "ref": "D1", "pin": "2", "net": "R_LED"},
        ],
    }
    llm = MockLLM(spec=SPEC, irs=[bad_ir], patches=[patch])
    agent = Agent(llm, parts, knowledge, tmp / "out2")
    res = agent.run("prompt", name="agent_led_button_bad")
    assert res.ok, (res.stage, res.log, res.pipeline.errors if res.pipeline else None)
    assert "patch" in llm.calls
    assert any("connected R1.2" in n for n in res.repairs)


def test_agent_refuses_out_of_scope(agent_env):
    parts, knowledge, tmp = agent_env
    llm = MockLLM(
        spec={**SPEC, "out_of_scope": True, "out_of_scope_reason": "AC mains requested"}
    )
    agent = Agent(llm, parts, knowledge, tmp / "out3")
    res = agent.run("220V AC 전원 회로 만들어줘")
    assert not res.ok
    assert res.stage == "refused"
    assert "mains" in res.refusal
    assert llm.calls == ["spec"]  # nothing was generated


def test_agent_stops_on_repeated_problems(agent_env):
    parts, knowledge, tmp = agent_env
    bad_ir = {
        **GOOD_IR,
        "name": "agent_led_button_stuck",
        "nets": [n for n in GOOD_IR["nets"] if n["name"] != "R_LED"],
    }
    noop_patch = {"analysis": "noop", "ops": []}
    llm = MockLLM(spec=SPEC, irs=[bad_ir], patches=[noop_patch, noop_patch, noop_patch])
    agent = Agent(llm, parts, knowledge, tmp / "out4")
    res = agent.run("prompt", name="agent_led_button_stuck")
    assert not res.ok
    # a useless patch leaves problems identical -> loop must stop early
    assert any("same problems twice" in line for line in res.log)
    assert llm.calls.count("patch") == 1


def test_duplicate_requirement_roles_are_made_unique():
    spec = {
        "parts_needed": [
            {"role": "Input Protection", "search_query": "Fuse"},
            {"role": "Input Protection", "search_query": "TVS"},
            {"role": "Input Protection", "search_query": "Bulk Capacitor"},
        ]
    }
    Agent._normalize_part_roles(spec)
    roles = [p["role"] for p in spec["parts_needed"]]
    assert len(roles) == len(set(roles)) == 3
    assert all(p["quantity"] == 1 for p in spec["parts_needed"])


def test_incompatible_stepper_candidate_is_rejected_for_bldc():
    need = {"role": "Motor Driver", "search_query": "BLDC motor driver"}
    hits = [
        {"lib_id": "Vendor:TC78H670FTG", "description": "dual stepper motor driver", "keywords": "stepper"},
        {"lib_id": "Vendor:DRV8323", "description": "three phase brushless gate driver", "keywords": "BLDC"},
    ]
    assert Agent._filter_incompatible_candidates(need, hits) == [hits[1]]


def test_series_regulator_request_rejects_shunt_reference_category():
    need = {"role": "regulator", "search_query": "voltage regulator"}
    hits = [
        {"lib_id": "Reference_Voltage:TL431DBZ", "description": "Shunt Regulator"},
        {"lib_id": "Regulator_Linear:Example", "description": "Linear regulator"},
    ]
    assert Agent._filter_incompatible_candidates(need, hits) == [hits[1]]


def test_simple_regulator_ranking_prefers_three_pin_complete_device():
    parts = PartIndex()
    agent = object.__new__(Agent)
    agent.parts = parts
    hits = parts.search_parts("5V linear voltage regulator", 12)
    ranked = agent._rank_simple_regulators(hits)
    pins = parts.get_part_pins(ranked[0]["lib_id"])
    names = {p["name"].upper() for p in pins}
    assert {"IN", "OUT", "GND"} <= names
    assert not any(p["type"] == "INPUT" for p in pins)


def test_a_role_that_names_a_declared_signal_is_a_net_not_a_bom_item():
    """In a schematic a net and a component are different objects, so the spec
    declares signals separately and the requirement itself says which is which.

    This replaced two word lists — "concept symbol" phrasings and thirteen
    terms for a terminal — that existed because "TX, RX 핀" produced roles
    tx_pin/rx_pin and the pipeline answered them with two diodes.
    """
    spec = {
        "signals": [{"name": "TX"}, {"name": "RX"}],
        "parts_needed": [
            {"role": "radio", "search_query": "module"},
            {"role": "tx_pin", "search_query": "pin"},
            {"role": "rx_pin", "search_query": "pin"},
        ],
        "connections_intent": [],
    }
    Agent._remove_connection_pseudo_parts(spec)
    assert [p["role"] for p in spec["parts_needed"]] == ["radio"]
    assert len(spec["connections_intent"]) == 2


def test_parts_survive_when_the_requirement_declares_no_signals():
    spec = {"parts_needed": [{"role": "radio", "search_query": "module"}]}
    Agent._remove_connection_pseudo_parts(spec)
    assert [p["role"] for p in spec["parts_needed"]] == ["radio"]


def test_explicit_conceptual_named_module_forces_catalog_miss():
    spec = {"parts_needed": [{"role": "radio", "search_query": "module", "value": "MY_CUSTOM_RADIO"}]}
    Agent._preserve_explicit_conceptual_parts("MY_CUSTOM_RADIO를 개념 심볼로 표시", spec)
    assert spec["parts_needed"][0]["search_query"] == "__conceptual__MY_CUSTOM_RADIO"


def test_conceptual_module_named_in_the_query_is_not_duplicated_into_a_second_role():
    """The extractor usually puts the module name in search_query, not value.

    Measured on unknown_module: matching only role/value appended a SECOND
    role for the same physical module, so the topology contract demanded two
    conceptual boxes for one part and aborted the run
    ("2 uncatalogued role(s) but only 1 conceptual device(s)").
    """
    spec = {"parts_needed": [
        {"role": "custom_radio_module", "search_query": "MY_CUSTOM_RADIO", "quantity": 1},
        {"role": "mcu", "search_query": "MCU", "quantity": 1},
    ]}
    Agent._preserve_explicit_conceptual_parts(
        "카탈로그에 없는 MY_CUSTOM_RADIO 모듈을 MCU에 연결해줘. 개념 심볼로 표시", spec
    )
    assert [p["role"] for p in spec["parts_needed"]] == ["custom_radio_module", "mcu"]
    assert spec["parts_needed"][0]["search_query"] == "__conceptual__MY_CUSTOM_RADIO"


def test_i2c_capability_filter_rejects_analog_temperature_sensor():
    parts = PartIndex(); agent = object.__new__(Agent); agent.parts = parts
    hits = [
        {"lib_id": "Sensor_Temperature:BD1020HFV"},
        {"lib_id": "Sensor_Temperature:Si7050-A20"},
    ]
    accepted = agent._parts_with_pins(hits, {"SDA", "SCL"})
    assert [h["lib_id"] for h in accepted] == ["Sensor_Temperature:Si7050-A20"]


def test_relay_capability_accepts_a1_a2_as_pin_numbers():
    parts = PartIndex(); agent = object.__new__(Agent); agent.parts = parts
    hits = [{"lib_id": "Relay:Relay_SPST-NO"}]
    assert agent._parts_with_pins(hits, {"A1", "A2"}) == hits


def test_repair_gate_duplicates_and_same_patch_adds(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(SPEC), parts, knowledge, tmp / "repair-gate")
    ir = CircuitIR("gate")
    ir.add(Component("R1", "Device:R", "4.7k"))
    ir.add(Component("SW1", "Switch:SW_Push", "SW_Push"))

    # generic passives are routine repair material: adding a second Device:R
    # and wiring it in the SAME patch must both survive the gate
    ops = [
        {"op": "add_component", "ref": "R2", "lib_id": "Device:R", "value": "4.7k"},
        {"op": "connect", "ref": "R2", "pin": "1", "net": "SDA"},
    ]
    kept, notes = agent._filter_ops(ir, ops, ["unconnected pin R1.1"])
    assert [op["op"] for op in kept] == ["add_component", "connect"]

    # duplicating a main device (IC/module lib_id) stays rejected
    kept, notes = agent._filter_ops(
        ir,
        [{"op": "add_component", "ref": "SW2", "lib_id": "Switch:SW_Push", "value": "SW"}],
        ["unconnected pin SW1.1"],
    )
    assert kept == []
    assert "duplicate" in notes[0]

    # connect to a ref that nothing in the patch adds stays rejected
    kept, notes = agent._filter_ops(
        ir,
        [{"op": "connect", "ref": "R9", "pin": "1", "net": "SDA"}],
        ["unconnected pin R1.1"],
    )
    assert kept == []
    assert "missing component" in notes[0]


def test_repair_gate_rejects_a_pin_the_symbol_does_not_have():
    """Measured on driver_relay, 3 of 3 seeds: "connected K1.4 to GND".

    Relay:G5V-1 numbers its pins 1/2/5/6/9/10 — there is no 3, 4, 7 or 8.
    The op passed every check in this gate (documented-NC, supply-driver,
    SKiDL conflict matrix) precisely BECAUSE the pin did not exist: the
    lookup raised KeyError, etype became None, and the whole validation
    block was skipped. Seed 202 put four such pins on GND in one round.
    """
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("relay")
    ir.add(Component("K1", "Relay:G5V-1", "G5V-1"))
    kept, notes = agent._filter_ops(
        ir,
        [
            {"op": "connect", "ref": "K1", "pin": "4", "net": "GND"},
            {"op": "connect", "ref": "K1", "pin": "6", "net": "GND"},
        ],
        ["unconnected pin K1.4", "unconnected pin K1.6"],
    )
    assert [op["pin"] for op in kept] == ["6"]
    assert any("has no such pin" in n for n in notes), notes

    # same check on a part the patch itself is adding, judged against the
    # lib_id the patch names rather than the (not yet existing) component
    kept, notes = agent._filter_ops(
        CircuitIR("empty"),
        [
            {"op": "add_component", "ref": "K1", "lib_id": "Relay:G5V-1", "value": "G5V-1"},
            {"op": "connect", "ref": "K1", "pin": "3", "net": "GND"},
            {"op": "connect", "ref": "K1", "pin": "6", "net": "GND"},
        ],
        ["unconnected pin K1.3"],
    )
    assert [op.get("pin") for op in kept] == [None, "6"]
    assert any("has no such pin" in n for n in notes), notes


def test_repair_gate_lets_a_conceptual_box_grow_a_pin():
    """The pin-existence check must not freeze an off-catalog module.

    A Conceptual: box has no library symbol; its pins ARE whatever the nets
    reference (conceptual.resolve_conceptual), so naming a new one is how
    the box legitimately grows a supply pin during repair.
    """
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("radio")
    ir.add(Component("U1", "Conceptual:MY_CUSTOM_RADIO", "MY_CUSTOM_RADIO"))
    ir.connect("UART_TX", ("U1", "TX"))
    kept, notes = agent._filter_ops(
        ir,
        [{"op": "connect", "ref": "U1", "pin": "VDD", "net": "+3V3"}],
        ["module supply pin U1.VDD is not on any rail"],
    )
    assert len(kept) == 1, notes


def test_repair_gate_drops_an_added_part_the_patch_never_wires():
    """Measured on driver_relay seed 202: R2..R12 added in one round.

    Seven of the eleven were never connected to anything, so they arrived on
    the board as floating parts — the round ADDED unconnected-pin errors to
    the problem list it was called to shorten (8 parts -> 23, ERC 2 -> 18).
    Nothing bounded this: Device:* is exempt from the duplicate check, and
    the "not part of any reported problem" check only reaches a ref that
    already exists, which a newly added one never does.
    """
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("flood")
    ir.add(Component("R1", "Device:R", "1k"))
    ops = [
        {"op": "add_component", "ref": f"R{n}", "lib_id": "Device:R", "value": "1k"}
        for n in range(2, 8)
    ]
    ops += [
        {"op": "connect", "ref": "R2", "pin": "1", "net": "+5V"},
        {"op": "connect", "ref": "R2", "pin": "2", "net": "SDA"},
    ]
    kept, notes = agent._filter_ops(ir, ops, ["unconnected pin R1.2"])
    assert sorted({op["ref"] for op in kept}) == ["R2"]
    assert [op["op"] for op in kept] == ["add_component", "connect", "connect"]
    assert any("never wires" in n for n in notes), notes


def test_repair_gate_drops_an_addition_whose_only_wiring_was_rejected():
    """The two gates compose: judged on the ops that SURVIVED, not the ones
    the model sent. An added relay wired only through a pin it does not have
    is an addition with no wiring at all."""
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    kept, notes = agent._filter_ops(
        CircuitIR("empty"),
        [
            {"op": "add_component", "ref": "K1", "lib_id": "Relay:G5V-1", "value": "G5V-1"},
            {"op": "connect", "ref": "K1", "pin": "3", "net": "GND"},
        ],
        ["unconnected pin K1.3"],
    )
    assert kept == []
    assert any("has no such pin" in n for n in notes), notes
    assert any("never wires" in n for n in notes), notes


def test_repair_gate_refuses_a_patch_that_removes_and_wires_the_same_part():
    """Measured on driver_relay seeds 201/203 once the phantom pins were gone.

    "removed D1" and "connected D1.1 to +5V" arrived in one patch. Filtering
    happens before anything is applied, so D1 still existed when the connect
    was judged; apply_patch removed the component (pruning its nodes) and
    then re-inserted ('D1','1') into the +5V net, leaving a net node pointing
    at a component that is not on the board.
    """
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("contradiction")
    ir.add(Component("D1", "Diode:1N4148", "1N4148"))
    ir.add(Component("R1", "Device:R", "1k"))
    kept, notes = agent._filter_ops(
        ir,
        [
            {"op": "remove_component", "ref": "D1"},
            {"op": "connect", "ref": "D1", "pin": "1", "net": "+5V"},
        ],
        ["unconnected pin D1.1", "unconnected pin D1.2"],
    )
    assert [op["op"] for op in kept] == ["connect"]
    assert any("also wires" in n for n in notes), notes

    # an uncontested removal is still allowed
    kept, notes = agent._filter_ops(
        ir, [{"op": "remove_component", "ref": "D1"}], ["unconnected pin D1.1"]
    )
    assert [op["op"] for op in kept] == ["remove_component"]


def test_repair_gate_checks_set_nc_the_op_name_the_schema_actually_emits():
    """The gate checked for "mark_nc" for its whole life; schemas.REPAIR_PATCH
    and ir_json._apply_one both call it "set_nc", so every NC op walked past
    the missing-component and pin-existence checks untouched."""
    from circuitgen.schemas import REPAIR_PATCH

    names = {
        branch["properties"]["op"]["const"]
        for branch in REPAIR_PATCH["properties"]["ops"]["items"]["anyOf"]
    }
    assert "set_nc" in names and "mark_nc" not in names

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("nc")
    ir.add(Component("K1", "Relay:G5V-1", "G5V-1"))
    kept, notes = agent._filter_ops(
        ir,
        [
            {"op": "set_nc", "ref": "K1", "pin": "7"},
            {"op": "set_nc", "ref": "K9", "pin": "1"},
            {"op": "set_nc", "ref": "K1", "pin": "9"},
        ],
        ["unconnected pin K1.9"],
    )
    assert [op["pin"] for op in kept] == ["9"]
    assert any("has no such pin" in n for n in notes), notes
    assert any("missing component" in n for n in notes), notes


def test_repeated_block_template_keeps_one_main_part_per_role():
    from circuitgen.ir import CircuitIR, Component

    ir = CircuitIR("driver_template")
    for n in range(1, 5):
        ir.add(Component(f"U{n}", "Driver_Motor:DRV8311H", "DRV8311H"))
        ir.connect(f"PWM{n}", (f"U{n}", "1"))
    notes = Agent._limit_template_copies(
        ir, {"driver": [{"lib_id": "Driver_Motor:DRV8311H"}]}
    )
    assert list(ir.components) == ["U1"]
    assert all(node[0] == "U1" for net in ir.nets for node in net.nodes)
    assert any("removed duplicate" in n for n in notes)


def test_repair_gate_rejects_output_pin_to_supply_net(agent_env):
    from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
    from circuitgen.pins import PinType

    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "out_gate")

    ir = CircuitIR("gate")
    ir.add(Component("U1", "X:ENC", "ENC"))
    ir.add(Component("#PWR01", "power:GND", "GND"))
    ir.connect("GND", ("#PWR01", "1"))

    enc = SymbolDef("X:ENC", "", [
        PinDef("3", "A", PinType.OUTPUT, 0, 0, 0, 2.54),
        PinDef("4", "CS", PinType.INPUT, 0, 0, 0, 2.54),
    ])
    gnd = SymbolDef("power:GND", "", [PinDef("1", "GND", PinType.PWRIN, 0, 0, 0, 2.54)], is_power=True)
    agent._resolve_symbols = lambda _ir: {"X:ENC": enc, "power:GND": gnd}

    ops = [
        {"op": "connect", "ref": "U1", "pin": "3", "net": "GND"},   # output -> GND: reject
        {"op": "connect", "ref": "U1", "pin": "3", "net": "+3V3"},  # output -> rail: reject
        {"op": "connect", "ref": "U1", "pin": "4", "net": "GND"},   # input -> GND: fine
    ]
    kept, notes = agent._filter_ops(ir, ops, ["unconnected pin U1.3", "unconnected pin U1.4"])
    assert [(o["pin"], o["net"]) for o in kept] == [("4", "GND")]
    assert sum("rejected op: connect U1.3" in n for n in notes) == 2


def test_limit_main_device_copies_respects_role_quantity(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(SPEC), parts, knowledge, tmp / "qty-gate")
    ir = CircuitIR("qty")
    for n in range(1, 6):  # 4 legitimate drivers + 1 repair-round duplicate
        ir.add(Component(f"U{n}", "Driver_Motor:DRV8311H", "DRV8311H"))
        ir.connect(f"PWM{n}", (f"U{n}", "1"))
    spec = {"parts_needed": [{"role": "bldc_motor_driver", "search_query": "x", "quantity": 4}]}
    notes = agent._limit_main_device_copies(
        ir, {"bldc_motor_driver": [{"lib_id": "Driver_Motor:DRV8311H"}]}, spec
    )
    assert sorted(ir.components) == ["U1", "U2", "U3", "U4"]
    assert any("beyond quantity 4" in n for n in notes)

    # a role absent from the spec is left untouched
    ir2 = CircuitIR("unknown_role")
    ir2.add(Component("U1", "Driver_Motor:DRV8311H", "D"))
    ir2.add(Component("U2", "Driver_Motor:DRV8311H", "D"))
    notes2 = agent._limit_main_device_copies(
        ir2, {"mystery": [{"lib_id": "Driver_Motor:DRV8311H"}]}, spec
    )
    assert sorted(ir2.components) == ["U1", "U2"] and notes2 == []


def test_repair_gate_rejects_error_level_pin_conflicts(agent_env):
    from circuitgen.ir import PinDef, SymbolDef
    from circuitgen.pins import PinType

    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "conflict-gate")
    ir = CircuitIR("c")
    ir.add(Component("U1", "X:ENC", "ENC1"))
    ir.add(Component("U2", "X:ENC", "ENC2"))
    ir.connect("SPI_MISO", ("U1", "3"))

    enc = SymbolDef("X:ENC", "", [
        PinDef("3", "MISO", PinType.OUTPUT, 0, 0, 0, 2.54),
        PinDef("4", "CS", PinType.INPUT, 0, 2.54, 0, 2.54),
    ])
    agent._resolve_symbols = lambda _ir: {"X:ENC": enc}

    ops = [
        {"op": "connect", "ref": "U2", "pin": "3", "net": "SPI_MISO"},  # OUTPUT x OUTPUT
        {"op": "connect", "ref": "U2", "pin": "4", "net": "SPI_MISO"},  # INPUT: fine
    ]
    kept, notes = agent._filter_ops(ir, ops, ["unconnected pin U2.3", "unconnected pin U2.4"])
    assert [(o["pin"], o["net"]) for o in kept] == [("4", "SPI_MISO")]
    assert any("conflicts with U1.3" in n for n in notes)


def test_pattern_synthesis_replaces_llm_for_matched_textbook_circuit(agent_env):
    parts, knowledge, tmp = agent_env
    spec = {
        "summary": "3.3V 비반전 증폭기",
        "power": {"rails": [{"name": "+3V3", "voltage": "3.3V"}, {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [
            {"role": "opamp", "search_query": "operational amplifier"},
            {"role": "feedback_resistor Rf", "search_query": "resistor", "value": "100k"},
            {"role": "ground_resistor Rg", "search_query": "resistor", "value": "10k"},
        ],
        "connections_intent": ["non-inverting amplifier, gain 11"],
    }
    llm = MockLLM(spec=spec)  # NO canned IR: pattern path must not need one
    agent = Agent(llm, parts, knowledge, tmp / "out-pattern")
    res = agent.run("3.3V 비반전 증폭 회로를 만들어줘", name="agent_noninv")
    assert any("pattern synthesis: noninverting_amplifier" in n for n in res.log), res.log
    assert llm.calls == ["spec"]  # no IR synthesis, no repairs
    assert res.ok, (res.stage, res.log[-8:], res.pipeline.errors if res.pipeline else None)
    assert res.pipeline.kicad_erc.ok
    assert res.pipeline.connectivity_ok
    # requested values flowed from the spec into the pattern params
    assert res.ir.components["R1"].value == "100k"
    assert res.ir.components["R2"].value == "10k"


def test_pattern_synthesis_maps_regulator_ports_to_spec_rails(agent_env):
    parts, knowledge, tmp = agent_env
    spec = {
        "summary": "12V to 5V linear regulator",
        "power": {"rails": [
            {"name": "+12V", "voltage": "12V"},
            {"name": "GND", "voltage": "0V"},
            {"name": "+5V", "voltage": "5V"},
        ]},
        "parts_needed": [
            {"role": "regulator", "search_query": "linear voltage regulator"},
            {"role": "input capacitor Cin", "search_query": "capacitor", "value": "10uF"},
            {"role": "output capacitor Cout", "search_query": "capacitor", "value": "22uF"},
        ],
        "connections_intent": ["12V in, 5V out, bypass caps both sides"],
    }
    llm = MockLLM(spec=spec)
    agent = Agent(llm, parts, knowledge, tmp / "out-reg-pattern")
    res = agent.run("12V 입력에서 5V를 만드는 레귤레이터 회로를 만들어줘", name="agent_ldo")
    assert any("pattern synthesis: ldo_linear_regulator" in n for n in res.log), res.log[-10:]
    assert llm.calls == ["spec"], llm.calls
    assert res.ok, (res.stage, res.log[-8:], res.pipeline.errors if res.pipeline else None)
    # regulator ports landed on the SPEC rails, not literal VIN/VOUT
    names = {n.name for n in res.ir.nets}
    assert "+12V" in names and "+5V" in names and "VIN" not in names


def test_pattern_synthesis_builds_complete_i2c_mcu_sensor_bus(agent_env):
    parts, knowledge, tmp = agent_env
    spec = {
        "summary": "STM32 MCU with I2C temperature sensor",
        "power": {"rails": [
            {"name": "+3V3", "voltage": "3.3V"},
            {"name": "GND", "voltage": "0V"},
        ]},
        "parts_needed": [
            {"role": "microcontroller", "search_query": "STM32 microcontroller"},
            {"role": "I2C temperature sensor", "search_query": "I2C temperature sensor"},
            {"role": "SDA pull-up", "search_query": "resistor", "value": "10k"},
            {"role": "SCL pull-up", "search_query": "resistor", "value": "10k"},
            {"role": "sensor decoupling", "search_query": "capacitor", "value": "100nF"},
        ],
        "connections_intent": ["I2C SDA/SCL with pull-ups and local decoupling"],
    }
    llm = MockLLM(spec=spec)
    agent = Agent(llm, parts, knowledge, tmp / "out-i2c-pattern")
    res = agent.run(
        "MCU에 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함",
        name="agent_i2c",
    )
    assert any("pattern synthesis: i2c_temperature_sensor" in n for n in res.log), res.log
    assert res.ok, (res.stage, res.log[-12:], res.pipeline.errors if res.pipeline else None)
    assert llm.calls == ["spec"]
    assert res.pipeline.kicad_erc.ok and res.pipeline.connectivity_ok

    mcu = next(r for r, c in res.ir.components.items() if "STM32G474" in c.lib_id)
    sensor = next(r for r, c in res.ir.components.items() if "Si7050" in c.lib_id)
    bus = [n for n in res.ir.nets if n.name not in {"+3V3", "GND"}
           and any(r == mcu for r, _ in n.nodes)
           and any(r == sensor for r, _ in n.nodes)]
    assert len(bus) == 2
    for net in bus:
        pullups = [r for r, _ in net.nodes if r.startswith("R")]
        assert pullups
        assert any(
            n.name == "+3V3" and any(r == pullups[0] for r, _ in n.nodes)
            for n in res.ir.nets
        )


I2C_SPEC = {
    "summary": "MCU with I2C temperature sensor",
    "power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]},
    "parts_needed": [
        {"role": "microcontroller", "search_query": "STM32 microcontroller"},
        {"role": "I2C temperature sensor", "search_query": "I2C temperature sensor"},
        {"role": "SDA pull-up", "search_query": "resistor", "value": "10k"},
        {"role": "SCL pull-up", "search_query": "resistor", "value": "10k"},
        {"role": "sensor decoupling", "search_query": "capacitor", "value": "100nF"},
    ],
    "connections_intent": ["I2C SDA/SCL with pull-ups and local decoupling"],
}


def test_pattern_refuses_to_substitute_a_part_the_user_named(agent_env):
    """A named part the pattern cannot hold must fall back, not be swapped.

    Measured before this gate: 'ESP32-C3 + BME280' came back as
    STM32G474 + Si7050 at ERC 0, reported as success.
    """
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "named-part")
    spec = {**I2C_SPEC, "parts_needed": [
        *I2C_SPEC["parts_needed"][:1],
        {"role": "sensor", "search_query": "TMP100"},
        *I2C_SPEC["parts_needed"][2:],
    ]}
    log: list[str] = []
    out = agent._pattern_synthesis(
        "MCU에 TMP100 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함", spec, "named", log
    )
    assert out is None
    assert any("declined: no role can hold requested part(s) TMP100" in n for n in log), log


def test_pattern_binds_the_named_part_instead_of_its_default(agent_env):
    parts, knowledge, tmp = agent_env
    llm = MockLLM(spec={**I2C_SPEC, "parts_needed": [
        *I2C_SPEC["parts_needed"][:1],
        {"role": "sensor", "search_query": "Si7051"},
        *I2C_SPEC["parts_needed"][2:],
    ]})
    agent = Agent(llm, parts, knowledge, tmp / "named-part-ok")
    res = agent.run(
        "MCU에 Si7051 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함", name="agent_si7051"
    )
    assert any("pattern synthesis: i2c_temperature_sensor" in n for n in res.log), res.log
    # the pattern pins Si7050-A20; the request named Si7051 and wins
    assert any("Si7051" in c.lib_id for c in res.ir.components.values())
    assert not any("Si7050" in c.lib_id for c in res.ir.components.values())
    assert res.ok, (res.stage, res.log[-8:])
    assert "Si7051" in res.compliance.satisfied_parts
    assert res.compliance.missing_parts == [] and res.compliance.ok


def test_header_roles_fall_back_to_generic_connectors(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "hdr")
    spec = {
        "summary": "uart debug",
        "power": {"rails": [{"name": "+3V3", "voltage": "3.3V"}, {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [{"role": "UART_DEBUG_HEADER", "search_query": "UART header", "quantity": 1}],
        "connections_intent": [],
    }
    candidates, _snippets, _pins = agent._gather(spec)
    hits = candidates["UART_DEBUG_HEADER"]
    assert hits, "header role must never come back empty"
    assert all(h["lib_id"].startswith("Connector_Generic:") for h in hits)


def test_conceptual_device_injected_for_uncatalogued_role(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "concept")
    spec = {
        "parts_needed": [
            {"role": "my_custom_radio", "search_query": "__conceptual__MY_CUSTOM_RADIO", "quantity": 1},
            {"role": "cap", "search_query": "capacitor", "value": "100nF"},
        ],
    }
    ir = CircuitIR("c")
    ir.add(Component("C1", "Device:C", "100nF"))
    log: list = []
    agent._ensure_conceptual_devices(
        ["my_custom_radio", "cap"], spec, ir,
        {"my_custom_radio": [], "cap": [{"lib_id": "Device:C"}]}, log,
    )
    boxes = [c for c in ir.components.values() if c.lib_id.startswith("Conceptual:")]
    assert len(boxes) == 1 and boxes[0].lib_id == "Conceptual:MY_CUSTOM_RADIO"
    assert any("conceptual device injected" in n for n in log)
    # idempotent
    agent._ensure_conceptual_devices(
        ["my_custom_radio"], spec, ir, {"my_custom_radio": []}, log,
    )
    assert sum(c.lib_id.startswith("Conceptual:") for c in ir.components.values()) == 1


def test_dropped_power_capacitor_is_restored_not_fatal(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "restore")
    spec = {
        "power": {"rails": [{"name": "+3V3", "voltage": "3.3V"}, {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [
            {"role": "power_capacitor", "search_query": "capacitor", "quantity": 1},
            {"role": "series_resistor", "search_query": "resistor", "value": "1k"},
        ],
    }
    ir = CircuitIR("r")
    ir.add(Component("U1", "Conceptual:X", "X"))  # model kept only the module
    log: list = []
    cands = {
        "power_capacitor": [{"lib_id": "Device:C", "reference_prefix": "C"}],
        "series_resistor": [{"lib_id": "Device:R", "reference_prefix": "R"}],
    }
    exempt = agent._restore_passive_roles(spec, ir, cands, log)
    # power cap restored across the rail; plain resistor exempted, not fatal
    assert "C1" in ir.components and ir.components["C1"].lib_id == "Device:C"
    nets = {n.name: n.nodes for n in ir.nets}
    assert ("C1", "1") in nets["+3V3"] and ("C1", "2") in nets["GND"]
    assert exempt == {"series_resistor"}
    assert any("restored dropped passive role" in n for n in log)


def test_pattern_synthesis_builds_can_interface(agent_env):
    parts, knowledge, tmp = agent_env
    spec = {
        "summary": "MCU CAN interface with transceiver, selectable termination, TVS and connector",
        "power": {"rails": [
            {"name": "+3V3", "voltage": "3.3V"},
            {"name": "GND", "voltage": "0V"},
            {"name": "+5V", "voltage": "5V"},
        ]},
        "parts_needed": [
            {"role": "microcontroller", "search_query": "STM32 microcontroller"},
            {"role": "CAN transceiver", "search_query": "CAN transceiver"},
            {"role": "termination resistor", "search_query": "resistor", "value": "120R"},
            {"role": "bus connector", "search_query": "connector"},
        ],
        "connections_intent": ["CAN bus with selectable 120R termination and TVS protection"],
    }
    llm = MockLLM(spec=spec)
    agent = Agent(llm, parts, knowledge, tmp / "out-can-pattern")
    res = agent.run(
        "MCU용 CAN 인터페이스를 만들어줘. 트랜시버, 종단 선택, TVS와 커넥터 포함",
        name="agent_can",
    )
    assert any("pattern synthesis: can_transceiver_interface" in n for n in res.log), res.log[-10:]
    assert llm.calls == ["spec"], llm.calls
    assert res.ok, (res.stage, res.log[-8:], res.pipeline.errors if res.pipeline else None)
    assert res.pipeline.kicad_erc.ok
    assert res.pipeline.connectivity_ok
    # transceiver VCC landed on the HIGHEST rail (TJA1051 is a 5V part)
    vcc_net = next(n for n in res.ir.nets if any(
        r == "U2" and p == "3" for r, p in n.nodes) or any(
        res.ir.components.get(r, None) and res.ir.components[r].lib_id.startswith("Interface_CAN_LIN")
        and p == "3" for r, p in n.nodes))
    assert vcc_net.name == "+5V", vcc_net.name
    # termination is jumper-selectable: R120 in series with the jumper header
    r_term = next(r for r, c in res.ir.components.items() if c.value == "120R")
    term_nets = [n.name for n in res.ir.nets if any(r == r_term for r, _p in n.nodes)]
    assert len(term_nets) == 2


def test_pattern_synthesis_builds_mcu_uart_debug(agent_env):
    parts, knowledge, tmp = agent_env
    spec = {
        "summary": "generic MCU minimal circuit with UART debug header and reset",
        "power": {"rails": [
            {"name": "+3V3", "voltage": "3.3V"},
            {"name": "GND", "voltage": "0V"},
        ]},
        "parts_needed": [
            {"role": "MCU", "search_query": "microcontroller"},
            {"role": "UART_DEBUG_HEADER", "search_query": "UART header"},
            {"role": "RESET_BUTTON", "search_query": "push button switch"},
            {"role": "DECOUPLING", "search_query": "capacitor", "value": "100nF", "quantity": 2},
        ],
        "connections_intent": ["UART debug header, reset button, decoupling"],
    }
    llm = MockLLM(spec=spec)
    agent = Agent(llm, parts, knowledge, tmp / "out-uart-pattern")
    res = agent.run(
        "범용 MCU 최소 회로와 UART 디버그 헤더, 리셋, 디커플링을 포함해줘",
        name="agent_uart",
    )
    assert any("pattern synthesis: mcu_uart_debug" in n for n in res.log), res.log[-10:]
    assert llm.calls == ["spec"], llm.calls
    assert res.ok, (res.stage, res.log[-8:], res.pipeline.errors if res.pipeline else None)
    assert res.pipeline.kicad_erc.ok
    assert res.pipeline.connectivity_ok
    # USART1 pins wired to the header; reset button on NRST
    nets = {n.name: [(r, p) for r, p in n.nodes] for n in res.ir.nets}
    tx = next(nodes for nodes in nets.values() if ("U1", "43") in nodes)
    assert any(r.startswith("J") for r, _p in tx)
    nrst = next(nodes for nodes in nets.values() if ("U1", "7") in nodes)
    assert any(r.startswith("SW") for r, _p in nrst)


def test_ambiguous_supply_pin_name_expands_to_the_whole_stack(agent_env):
    """MC68332 has 13 pins named VDD, so "connect this net to VDD" was
    ambiguous, left unresolved, and reported as unknown_pin U1.VDD."""
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "expand")
    ir = CircuitIR("stack")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx"))
    ir.connect("+3V3", ("U1", "VDD"))
    ir.connect("GND", ("U1", "VSS"))

    notes = agent.resolve_pin_names(ir)
    sym = agent._resolve_symbols(ir)["MCU_ST_STM32G4:STM32G474RETx"]
    vdd = {p.number for p in sym.pins if p.name.upper() == "VDD"}
    # KiCad types the hidden duplicates of a stack PASSIVE (VSS 31/47/63);
    # only the visible supply pin is wired here, unify_stacked_pins joins
    # the rest by coordinate afterwards
    vss_visible = {
        p.number for p in sym.pins
        if p.name.upper() == "VSS" and not p.hidden
    }
    nets = {n.name: {p for r, p in n.nodes if r == "U1"} for n in ir.nets}
    assert len(vdd) > 1, "fixture must actually have a stacked supply"
    assert nets["+3V3"] == vdd
    assert nets["GND"] == vss_visible and "VSS" not in nets["GND"]
    assert any("stacked supply pin" in n for n in notes)


def test_ambiguous_signal_pin_name_is_not_expanded(agent_env):
    """Two signal pins sharing a name are not a stack — tying them together
    would invent a connection the request never asked for."""
    from circuitgen.ir import PinDef, SymbolDef
    from circuitgen.pins import PinType

    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "expand2")
    sym = SymbolDef(
        "Test:DUAL", "",
        [PinDef("1", "A", PinType.PASSIVE, 0, 0, 0, 2.54),
         PinDef("2", "A", PinType.PASSIVE, 0, 0, 0, 2.54),
         PinDef("3", "K", PinType.PASSIVE, 0, 0, 0, 2.54)],
    )
    ir = CircuitIR("dual")
    ir.add(Component("D1", "Test:DUAL", "DUAL"))
    ir.connect("N1", ("D1", "A"))
    agent._resolve_symbols = lambda _ir: {"Test:DUAL": sym}

    agent.resolve_pin_names(ir)
    assert [p for r, p in ir.nets[0].nodes] == ["A"], "left alone, so ERC stays loud"


def test_mains_acdc_converters_are_outside_the_declared_scope():
    """extract_requirements declares "max 24VDC / 3A, no AC mains" and refuses
    a request that needs mains outright, so a mains converter can never be a
    valid candidate for a request that got past that gate.

    Measured: a "3.3V 단일 전원" MCU board selected Converter_ACDC:HS-40003,
    whose AC/L pin then sat on a signal net.
    """
    need = {"role": "power_supply", "search_query": "3.3V power supply"}
    hits = [
        {"lib_id": "Converter_ACDC:HS-40003", "description": "AC/DC converter 3.3V"},
        {"lib_id": "Regulator_Linear:AMS1117-3.3", "description": "3.3V LDO"},
    ]
    kept = Agent._filter_incompatible_candidates(need, hits)
    assert [h["lib_id"] for h in kept] == ["Regulator_Linear:AMS1117-3.3"]


def test_the_normalization_sequence_is_one_sequence_and_is_idempotent(agent_env):
    """There used to be two: 31 passes after synthesis and 12 after each repair
    round, so a repaired board was normalized by a different rule set — 19
    passes never ran on repaired circuits. Merging them is only safe if every
    pass is idempotent, so that is asserted here rather than assumed.
    """
    from collections import Counter

    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "idem")
    lid = "MCU_ST_STM32G4:STM32G474RETx"
    spec = {
        "power": {"rails": [{"name": "+3V3", "voltage": "3.3V"},
                            {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [{"role": "mcu", "search_query": "STM32G474RET6"}],
    }
    ir = CircuitIR("idem")
    ir.add(Component("U1", lid, "STM32G474RET6", group="MCU"))
    ir.connect("+3V3", ("U1", "16"))
    ir.connect("GND", ("U1", "15"))

    def snapshot():
        return (
            len(ir.components), len(ir.nets), len(ir.nc_pins),
            dict(Counter(c.value for c in ir.components.values() if c.lib_id == "Device:C")),
        )

    agent._normalize(ir, spec, "STM32G474RET6 최소 회로")
    first = snapshot()
    agent._normalize(ir, spec, "STM32G474RET6 최소 회로")
    agent._normalize(ir, spec, "STM32G474RET6 최소 회로")
    assert snapshot() == first, "running the sequence again must change nothing"


def test_named_parts_rewrite_the_role_that_was_looking_for_them(agent_env):
    """The product assumption is that the user already chose the parts, so a
    part number must drive SELECTION, not just after-the-fact grading.

    Measured on driver_relay: the prompt named Relay:G5V-1, BC337 and 1N4148,
    the extractor reduced them to "relay"/"transistor"/"diode", and the board
    came out with none of the three while Relay:G5V-1 sat unused in the
    bundled library.

    Which role a part belongs to is decided by the catalog and by IEEE 315
    reference designators, never by a synonym table.
    """
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "named")

    spec = {"parts_needed": [
        {"role": "mcu", "search_query": "STM32 microcontroller"},
        {"role": "sensor", "search_query": "I2C temperature sensor"},
    ]}
    agent._ensure_named_parts("STM32G474RET6에 온도센서 TMP100을 연결", spec)
    assert [(p["role"], p["search_query"]) for p in spec["parts_needed"]] == [
        ("mcu", "STM32G474RET6"), ("sensor", "TMP100"),
    ], "an MCU and a sensor are both 'U'; one must not take the other's role"


def test_a_named_part_with_no_matching_role_becomes_its_own(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "named2")
    spec = {"parts_needed": [{"role": "led", "search_query": "LED"}]}
    agent._ensure_named_parts("STM32G474RET6로 제어합니다", spec)
    roles = [(p["role"], p["search_query"]) for p in spec["parts_needed"]]
    assert ("led", "LED") in roles
    assert any(q == "STM32G474RET6" for _r, q in roles)


def test_a_word_that_only_looks_like_a_part_number_is_not_selected(agent_env):
    """The catalog decides what is a part number; RS485 is a protocol."""
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "named3")
    spec = {"parts_needed": [{"role": "xcvr", "search_query": "transceiver"}]}
    agent._ensure_named_parts("RS485(Modbus RTU) 통신이 필요합니다", spec)
    assert [p["search_query"] for p in spec["parts_needed"]] == ["transceiver"]


def test_capability_filters_never_override_a_part_the_user_named(agent_env):
    """The filters exist to pick among generic search results. They have no
    authority over an explicit choice.

    Measured: Relay:G5V-1 numbers its coil pins 1/2/5/6/9/10 with blank names,
    the relay branch of _gather requires pins called A1/A2, so the user's relay
    was discarded and Relay:RM50-xx21 substituted — while the board still
    reported the request as satisfied because the substitute carried "G5V-1"
    in its value field.
    """
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "gather")
    agent.parts = PartIndex()          # the full catalog, not the test subset
    spec = {
        "summary": "relay driver", "connections_intent": [],
        "power": {"rails": [{"name": "+12V"}, {"name": "GND"}]},
        "parts_needed": [
            {"role": "relay", "search_query": "G5V-1"},
            {"role": "generic relay", "search_query": "relay"},
        ],
    }
    candidates, _snips, _pins = agent._gather(spec)
    assert [h["lib_id"] for h in candidates["relay"]] == ["Relay:G5V-1"]
    # a generic query is still filtered — "relay" is also the NAME of a symbol
    # in the OLIMEX library, and matching on the name alone made every generic
    # query look like an explicit choice
    generic = [h["lib_id"] for h in candidates["generic relay"]]
    assert generic and all(g.startswith("Relay:") for g in generic)
    assert "Relay:G5V-1" not in generic
