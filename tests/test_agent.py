"""Agent orchestrator against a mock LLM backend.

Everything except the model is real: part index (small fixture), knowledge
index, self-ERC, emitter, kicad-cli oracle. The mock returns canned
schema-shaped payloads, letting us test the staging, validation and
repair-loop mechanics deterministically. The real-model end-to-end run
lives in scripts/run_agent.py (needs llama-server with a loaded model).
"""

import copy
import json
from pathlib import Path

import pytest

from circuitgen.agent import Agent, repair_problem_list, request_mode
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.knowledge import KNOWLEDGE_DIR, KnowledgeIndex, build_index as build_kn
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.partindex import LibrarySource, PartIndex, build_index as build_parts
from circuitgen.patterns import load_patterns
from circuitgen.symbols import KICAD_SYMBOL_DIR

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

FIXTURE_PATTERN_DIR = Path(__file__).resolve().parent / "fixtures" / "patterns"


def test_request_mode_requires_explicit_reference_pin_members():
    assert request_mode(
        "Netlist: VCC: U1(8:VCC), J1(1); GND: U1(1:GND), J1(2)."
    ) == "transcription"
    assert request_mode(
        "STM32와 센서를 I2C로 연결하려고 합니다. S 핀 처리와 풀업 값을 설계해주세요."
    ) == "design"


def test_request_mode_accepts_pin_word_notation_without_net_name_vocabulary():
    assert request_mode("alpha = J1 Pin 1, U1 Pin 3; beta = J1 핀 2, U1 핀 1") == "transcription"


def test_physical_role_normalization_is_general_and_idempotent():
    spec = {"parts_needed": [
        {"role": "regulator", "search_query": "AMS1117-3"},
        {"role": "package", "search_query": "SOT-223"},
        {"role": "input", "search_query": "header", "value": "2-pin header"},
        {"role": "programmer", "search_query": "connector", "value": "2x3"},
    ]}
    Agent._normalize_physical_roles(spec)
    once = json.loads(json.dumps(spec))
    Agent._normalize_physical_roles(spec)
    assert spec == once
    assert [part["role"] for part in spec["parts_needed"]] == [
        "regulator", "input", "programmer",
    ]
    assert spec["parts_needed"][1]["search_query"] == "1x2 pin header"
    assert spec["parts_needed"][2]["search_query"] == "2x3 connector"


def test_passive_value_search_query_is_rehomed_to_catalog_class():
    """Measured: 10kΩ / 8Ω as search_query became Conceptual:10k / Conceptual:8."""
    spec = {"parts_needed": [
        {"role": "potentiometer", "search_query": "10kΩ", "quantity": 1},
        {"role": "speaker", "search_query": "8Ω", "quantity": 1},
        {"role": "crystal", "search_query": "16M", "quantity": 1},
        {"role": "timer", "search_query": "NE555D", "quantity": 1},
    ]}
    Agent._normalize_physical_roles(spec)
    assert spec["parts_needed"][0]["search_query"] == "potentiometer"
    assert spec["parts_needed"][0]["value"] == "10kΩ"
    assert spec["parts_needed"][1]["search_query"] == "speaker"
    assert spec["parts_needed"][1]["value"] == "8Ω"
    # Role is the search — do not invent "resistor" from a value synonym table.
    assert spec["parts_needed"][2]["search_query"] == "crystal"
    assert spec["parts_needed"][2]["value"] == "16M"
    assert spec["parts_needed"][3]["search_query"] == "NE555D"


def test_search_parts_resolves_full_library_symbol_id():
    from circuitgen.partindex import PartIndex

    idx = PartIndex()
    hits = idx.search_parts("Switch:SW_Push", limit=3)
    assert hits and hits[0]["lib_id"] == "Switch:SW_Push"


def enable_internal_pattern_fixtures(agent: Agent) -> None:
    """Opt a pattern-engine test into archived, non-production fixtures."""
    agent._patterns = load_patterns(
        FIXTURE_PATTERN_DIR, allow_internal_fixtures=True
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


def test_mcu_requirement_preserves_an_explicit_5v_logic_rail():
    spec = {
        "power": {"rails": [
            {"name": "+5V", "voltage": "5V"}, {"name": "GND", "voltage": "0V"},
        ]},
        "parts_needed": [{"role": "controller", "search_query": "microcontroller"}],
    }
    Agent._ensure_logic_rail(spec)
    assert [r["name"] for r in spec["power"]["rails"]] == ["+5V", "GND"]


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
        "Connector", "Connector_Generic", "Regulator_Linear", "MCU_ST_STM32G4",
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
    # Deep copy: extraction writes rails parsed from the prompt into the spec it
    # is handed, and a shallow {**SPEC} shares the nested power dict — this run
    # used to leave "+220V" in the module-level SPEC for every later test.
    llm = MockLLM(
        spec={
            **copy.deepcopy(SPEC),
            "out_of_scope": True,
            "out_of_scope_reason": "AC mains requested",
        }
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


class RecordingLLM(MockLLM):
    """MockLLM that also keeps the user prompt of every call."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: list[str] = []

    def complete_json(self, messages, schema, **kw):
        self.prompts.append(messages[-1]["content"])
        return super().complete_json(messages, schema, **kw)


def test_repair_prompt_names_the_op_that_answers_the_problem(agent_env):
    """The replacement recipe belongs to unknown_symbol problems only.

    Offered on every round it was this prompt's one worked example, and the
    model copied it everywhere else: measured across four campaign domains,
    89 of 115 repair ops re-added a part under the ref and lib_id it already
    had, each rejected by the gate, so the loop connected nothing.
    """
    parts, knowledge, tmp = agent_env
    llm = RecordingLLM(patches=[{"ops": []}, {"ops": []}, {"ops": []}])
    agent = Agent(llm, parts, knowledge, tmp / "repair-prompt")
    ir = CircuitIR("prompt-shape")
    ir.add(Component("R1", "Device:R", "330R"))

    agent._repair(ir, ["component_does_no_work: R1 pin 2 is on no net at all"], {})
    unconnected = llm.prompts[-1]
    assert "add_component with the same ref" not in unconnected
    assert "connect op" in unconnected and "set_nc" in unconnected

    agent._repair(ir, ["unknown_symbol: R1 lib_id 'Device:Rx' not in library set"], {})
    assert "add_component with the same ref" in llm.prompts[-1]

    # ...and a board with no unconnected pin is not told how to connect one.
    ir.connect("SIG", ("R1", "1"))
    ir.connect("GND", ("R1", "2"))
    agent._repair(ir, ["power_pin_unpowered: nothing drives SIG"], {})
    assert "connect op" not in llm.prompts[-1]


def test_repair_prompt_includes_already_gathered_knowledge(agent_env):
    parts, knowledge, tmp = agent_env
    llm = RecordingLLM(patches=[{"ops": []}])
    agent = Agent(llm, parts, knowledge, tmp / "repair-kn")
    ir = CircuitIR("kn")
    ir.add(Component("U2", "Sensor_Temperature:TMP100", "TMP100"))
    agent._repair_knowledge = [{
        "id": "tmp100-i2c-pullup-and-bypass",
        "statement": "V+ (pin 4) is 2.7 V to 5.5 V",
    }]
    agent._repair(ir, ["power_pin_misses_requested_rail: U2.4 (V+) is on SCL"], {})
    prompt = llm.prompts[-1]
    assert "KNOWLEDGE" in prompt
    assert "tmp100-i2c-pullup-and-bypass" in prompt


def test_repair_problem_list_uses_compliance_when_erc_is_clean():
    from circuitgen.compliance import ComplianceReport
    from circuitgen.ir import ValidationIssue
    from circuitgen.pipeline import PipelineResult

    pr = PipelineResult(ok=True)
    report = ComplianceReport(issues=[
        ValidationIssue(
            "compliance", "power_pin_misses_requested_rail", "error",
            "U2.4", "supply pin U2.4 (V+) is on SCL, which is not any requested rail (+3V3)",
        )
    ])
    problems = repair_problem_list(pr, report)
    assert problems and problems[0].startswith("power_pin_misses_requested_rail")

    erc_pr = PipelineResult(ok=False, self_erc=[
        ValidationIssue("circuitgen-erc", "unconnected_pin", "error", "U2.4", "pin on no net")
    ])
    erc_problems = repair_problem_list(erc_pr, report)
    assert erc_problems[0].startswith("unconnected_pin")
    assert not any("power_pin_misses_requested_rail" in p for p in erc_problems)

    clean = repair_problem_list(PipelineResult(ok=True), ComplianceReport())
    assert clean == []


def test_rejected_repair_round_retries_with_the_gate_reason(agent_env):
    """A wholly rejected round is not the same question asked twice."""
    parts, knowledge, tmp = agent_env
    bad_ir = {
        **GOOD_IR,
        "name": "agent_led_button_rejected",
        "nets": [n for n in GOOD_IR["nets"] if n["name"] != "R_LED"],
    }
    self_replacement = {
        "analysis": "R1 looks wrong",
        "ops": [{"op": "add_component", "ref": "R1", "lib_id": "Device:R", "value": "330R"}],
    }
    real_fix = {
        "analysis": "the series link is missing",
        "ops": [
            {"op": "connect", "ref": "R1", "pin": "2", "net": "R_LED"},
            {"op": "connect", "ref": "D1", "pin": "2", "net": "R_LED"},
        ],
    }
    # SPEC's nested dicts are shared across this module and earlier runs write
    # prompt-derived rails into them; take a private copy.
    llm = RecordingLLM(
        spec=copy.deepcopy(SPEC), irs=[bad_ir], patches=[self_replacement, real_fix]
    )
    agent = Agent(llm, parts, knowledge, tmp / "repair-retry")
    res = agent.run("prompt", name="agent_led_button_rejected")
    assert llm.calls.count("patch") == 2
    assert "previous patch was rejected" in llm.prompts[-1]
    assert "replacement of valid R1" in llm.prompts[-1]
    assert res.ok, (res.stage, res.log)


def test_repair_stops_when_the_rejection_reason_repeats(agent_env):
    """Feedback buys one informed retry, not an unbounded loop."""
    parts, knowledge, tmp = agent_env
    bad_ir = {
        **GOOD_IR,
        "name": "agent_led_button_stubborn",
        "nets": [n for n in GOOD_IR["nets"] if n["name"] != "R_LED"],
    }
    self_replacement = {
        "analysis": "R1 looks wrong",
        "ops": [{"op": "add_component", "ref": "R1", "lib_id": "Device:R", "value": "330R"}],
    }
    llm = RecordingLLM(
        spec=copy.deepcopy(SPEC),
        irs=[bad_ir],
        patches=[self_replacement, self_replacement, self_replacement],
    )
    agent = Agent(llm, parts, knowledge, tmp / "repair-stubborn")
    res = agent.run("prompt", name="agent_led_button_stubborn")
    assert not res.ok
    assert llm.calls.count("patch") == 2
    assert any("same problems twice" in line for line in res.log)


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

    # Device:LED is a requested device (prefix D), not a pullup
    ir.add(Component("D1", "Device:LED", "LED"))
    kept, notes = agent._filter_ops(
        ir,
        [
            {"op": "add_component", "ref": "D2", "lib_id": "Device:LED", "value": "LED"},
            {"op": "connect", "ref": "D2", "pin": "1", "net": "R_LED"},
            {"op": "connect", "ref": "D2", "pin": "2", "net": "GND"},
        ],
        ["unconnected pin D1.2"],
    )
    assert kept == []
    assert any("duplicate" in n and "Device:LED" in n for n in notes)

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


def test_apply_patch_does_not_insert_nodes_for_a_missing_ref():
    """Measured on the timer campaign board: add U2 was rejected as a
    duplicate NE555, but connect U2.2 to TRIG still wrote a net node.
    Conduction counted that ghost as the other member of U1.2, so a lonely
    control pin scored as working."""
    from circuitgen.ir_json import apply_patch

    ir = CircuitIR("ghost")
    ir.add(Component("U1", "Timer:NE555D", "NE555"))
    ir.connect("TRIG", ("U1", "2"))
    notes = apply_patch(ir, [{"op": "connect", "ref": "U2", "pin": "2", "net": "TRIG"}])
    members = {(r, p) for net in ir.nets for r, p in net.nodes}
    assert ("U2", "2") not in members
    assert any("not on the board" in n for n in notes), notes


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


def test_block_budget_is_pin_derived_not_the_schema_ceiling(agent_env):
    """A 2-pin speaker must not inherit CIRCUIT_IR.maxItems=30 as a keep-all quota."""
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "budget")
    speaker = agent._block_component_budget(
        {"speaker": [{"lib_id": "Device:Speaker"}], "support_passives": [{"lib_id": "Device:R"}]}
    )
    assert speaker < 30
    assert speaker >= 8
    mcu_hits = parts.search_parts("STM32G474", limit=1)
    if mcu_hits:
        mcu = agent._block_component_budget({"controller": [mcu_hits[0]]})
        assert mcu >= speaker


def test_block_overflow_keeps_the_role_device_and_drops_schema_fill(agent_env):
    """Measured: speaker block of 1 role emitted exactly 30 Device:R."""
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "overflow")
    ir = CircuitIR("speaker")
    ir.add(Component("LS1", "Device:Speaker", "8ohm"))
    for n in range(1, 30):
        ir.add(Component(f"R{n}", "Device:R", "10k"))
        ir.connect("IN", (f"R{n}", "1"))
    candidates = {
        "speaker": [{"lib_id": "Device:Speaker"}],
        "support_passives": [{"lib_id": "Device:R"}],
    }
    budget = agent._block_component_budget(candidates)
    notes = agent._trim_block_overflow(ir, candidates, budget)
    assert "LS1" in ir.components
    assert ir.components["LS1"].lib_id == "Device:Speaker"
    assert len([r for r in ir.components if not r.startswith("#")]) <= budget
    assert sum(1 for c in ir.components.values() if c.lib_id == "Device:R") == 0
    assert any("block overflow" in n for n in notes)
    # Nets of dropped resistors must not keep dangling refs.
    leftover = set(ir.components)
    assert all(node[0] in leftover for net in ir.nets for node in net.nodes)


def test_block_overflow_is_noop_under_budget(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "under")
    ir = CircuitIR("speaker")
    ir.add(Component("LS1", "Device:Speaker", "8ohm"))
    for n in range(1, 3):
        ir.add(Component(f"R{n}", "Device:R", "10k"))
    for n in range(1, 4):
        ir.add(Component(f"C{n}", "Device:C", "100nF"))
    candidates = {
        "speaker": [{"lib_id": "Device:Speaker"}],
        "support_passives": [{"lib_id": "Device:R"}, {"lib_id": "Device:C"}],
    }
    budget = agent._block_component_budget(candidates)
    notes = agent._trim_block_overflow(ir, candidates, budget)
    assert notes == []
    assert sorted(ir.components) == ["C1", "C2", "C3", "LS1", "R1", "R2"]


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


def test_limit_main_device_copies_trims_leds_not_resistors():
    """Device:LED used to skip the quantity trim because every Device:* did.

    A quantity-1 LED role then kept the extra D2 repair added and shorted.
    Prefix R/C/L are still allowed to exceed quantity (several pullups).
    """
    agent = object.__new__(Agent)
    ir = CircuitIR("led-qty")
    ir.add(Component("D1", "Device:LED", "LED"))
    ir.add(Component("D2", "Device:LED", "LED"))
    ir.add(Component("R1", "Device:R", "330"))
    ir.add(Component("R2", "Device:R", "10k"))
    ir.connect("ANODE", ("D1", "2"), ("R1", "2"))
    ir.connect("GND", ("D1", "1"))
    ir.connect("X", ("D2", "1"), ("D2", "2"))
    spec = {
        "parts_needed": [
            {"role": "led", "quantity": 1},
            {"role": "resistor", "quantity": 1},
        ]
    }
    notes = agent._limit_main_device_copies(
        ir,
        {
            "led": [{"lib_id": "Device:LED", "reference_prefix": "D"}],
            "resistor": [{"lib_id": "Device:R", "reference_prefix": "R"}],
        },
        spec,
    )
    assert "D1" in ir.components and "D2" not in ir.components, notes
    assert "R1" in ir.components and "R2" in ir.components
    assert any("beyond quantity 1" in n and "D2" in n for n in notes)


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
            {"role": "regulator", "search_query": "linear voltage regulator", "functional_kind": "voltage_regulator"},
            {"role": "input capacitor Cin", "search_query": "capacitor", "value": "10uF", "functional_kind": "input_bypass_capacitor"},
            {"role": "output capacitor Cout", "search_query": "capacitor", "value": "22uF", "functional_kind": "output_bypass_capacitor"},
        ],
        "connections_intent": ["12V in, 5V out, bypass caps both sides"],
    }
    llm = MockLLM(spec=spec)
    agent = Agent(llm, parts, knowledge, tmp / "out-reg-pattern")
    res = agent.run("12V 입력에서 5V를 만드는 레귤레이터 회로를 만들어줘", name="agent_ldo")
    assert any("typed rule graph match: ldo_linear_regulator" in n for n in res.log), res.log
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
    enable_internal_pattern_fixtures(agent)
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
    enable_internal_pattern_fixtures(agent)
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
    enable_internal_pattern_fixtures(agent)
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


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "data" / "datasheets" / "tmp100_SBOS231I.pdf").is_file(),
    reason="TMP100 datasheet PDF is local-only (gitignored)",
)
def test_gather_injects_datasheet_knowledge_for_named_parts(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "kn")
    spec = {
        "summary": "STM32G474RET6 and TMP100 on 3.3V I2C",
        "power": {
            "rails": [
                {"name": "+3V3", "voltage": "3.3V"},
                {"name": "GND", "voltage": "0V"},
            ]
        },
        "parts_needed": [
            {"role": "MCU", "search_query": "STM32G474RET6", "quantity": 1},
            {"role": "TEMP_SENSOR", "search_query": "TMP100", "quantity": 1},
            {"role": "i2c_pullup", "search_query": "resistor", "quantity": 2},
        ],
        "connections_intent": ["SDA SCL pullup", "address pins"],
    }
    _candidates, snippets, _pins = agent._gather(spec)
    ids = {s["id"] for s in snippets}
    assert "tmp100-i2c-pullup-and-bypass" in ids, ids
    assert "tmp100-address-pins" in ids, ids
    topics = agent._knowledge_trace[-1]["topics"]
    assert "MCU" not in topics and "TEMP_SENSOR" not in topics, topics
    assert "resistor" not in topics, topics
    assert "SDA SCL pullup" not in topics, topics
    assert "address pins" not in topics, topics
    assert "select-inamp-vs-single-opamp-difference-amp" not in ids


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


def test_catalog_connector_hits_are_not_replaced_with_generic_headers(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "usb")
    spec = {
        "summary": "usb device",
        "power": {"rails": [{"name": "+5V", "voltage": "5V"}, {"name": "GND", "voltage": "0V"}]},
        "parts_needed": [{"role": "port", "search_query": "USB-C connector", "quantity": 1}],
        "connections_intent": [],
    }
    candidates, _snippets, _pins = agent._gather(spec)
    hits = candidates["port"]
    assert hits, "a catalog USB-C query must return the parts the index found"
    assert any("USB_C" in h["lib_id"] for h in hits), [h["lib_id"] for h in hits]
    assert not any(
        h["lib_id"].startswith("Connector_Generic:Conn_01x") for h in hits
    ), [h["lib_id"] for h in hits]


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
    # power cap restored across the rail; plain resistor exempted, not placed floating
    assert "C1" in ir.components and ir.components["C1"].lib_id == "Device:C"
    nets = {n.name: n.nodes for n in ir.nets}
    assert ("C1", "1") in nets["+3V3"] and ("C1", "2") in nets["GND"]
    assert exempt == {"series_resistor"}
    assert "R1" not in ir.components
    assert any("restored dropped passive role" in n for n in log)


def test_dropped_potentiometer_and_speaker_are_placed_not_exempted(agent_env):
    """Catalogued non-resistor passives are the requested device.

    Exempting them left campaign audio boards with no speaker after the
    model filled the template with Device:R. Generic R stays exempt.
    """
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "pot-speaker")
    spec = {
        "parts_needed": [
            {"role": "potentiometer", "search_query": "potentiometer", "value": "10kΩ"},
            {"role": "speaker", "search_query": "speaker", "value": "8Ω"},
        ],
    }
    ir = CircuitIR("audio")
    ir.add(Component("U1", "Amplifier_Audio:LM386", "LM386"))
    log: list = []
    cands = {
        "potentiometer": [
            {"lib_id": "Simulation_SPICE:Potentiometer", "reference_prefix": "R"},
            {"lib_id": "Device:R_Potentiometer", "reference_prefix": "RV"},
        ],
        "speaker": [{"lib_id": "Device:Speaker", "reference_prefix": "LS"}],
    }
    exempt = agent._restore_passive_roles(spec, ir, cands, log)
    assert exempt == set()
    assert ir.components["RV1"].lib_id == "Device:R_Potentiometer"
    assert ir.components["RV1"].value == "10kΩ"
    assert ir.components["LS1"].lib_id == "Device:Speaker"
    assert ir.components["LS1"].value == "8Ω"
    # Unwired — repair connects; do not invent a net.
    placed = {"RV1", "LS1"}
    assert not any(r in placed for net in ir.nets for r, _p in net.nodes)
    assert any("placed dropped passive role 'speaker'" in n for n in log)
    # idempotent
    agent._restore_passive_roles(spec, ir, cands, log)
    assert sum(c.lib_id == "Device:Speaker" for c in ir.components.values()) == 1
    assert sum(c.lib_id == "Device:R_Potentiometer" for c in ir.components.values()) == 1


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
    enable_internal_pattern_fixtures(agent)
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
    enable_internal_pattern_fixtures(agent)
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


def test_duplicate_removal_keeps_the_better_wired_copy():
    """Measured on the 4-motor board: the MCU block produced U1 wired to every
    motor and encoder net, the UART block separately produced MCU1 carrying
    only TXD/RXD, and `sorted()` kept MCU1 — deleting the controller the rest
    of the board was connected to, and every one of those connections with it.
    """
    agent = object.__new__(Agent)
    ir = CircuitIR("two-mcus")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474"))
    ir.add(Component("MCU1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474"))
    for pin, net in (("21", "MOTOR1_PWM_A"), ("22", "MOTOR2_PWM_A"),
                     ("23", "ENC1_CS"), ("24", "ENC2_CS")):
        ir.connect(net, ("U1", pin))
    ir.connect("UART_TX", ("MCU1", "8"))

    spec = {"parts_needed": [{"role": "controller", "quantity": 1}]}
    cands = {"controller": [{"lib_id": "MCU_ST_STM32G4:STM32G474RETx"}]}
    notes = agent._limit_main_device_copies(ir, cands, spec)

    assert set(ir.components) == {"U1"}, ir.components
    assert any("kept U1 (4)" in n for n in notes), notes
    # the wiring survived with it
    assert {n.name for n in ir.nets} == {
        "MOTOR1_PWM_A", "MOTOR2_PWM_A", "ENC1_CS", "ENC2_CS"
    }


def test_a_request_that_carries_its_netlist_is_transcribed_not_designed():
    """Three fully specified requests — an LDO, an NE555 astable, an ATmega
    minimal board — each handed over a complete net list, and each was put
    through the design pipeline: the planner turned "SOT-223" (a package) and
    "22uF" (a value) into roles, then into blocks, then tried to synthesize a
    sub-circuit for each. Two of the three ended with no schematic at all.

    SKiDL's contract is the right one here: the person who wrote
    `vin & r1 & vout` meant it. Transcription makes no design decision, and
    unlike "is this a good design?" its correctness has an exact answer —
    every reference and pin the user wrote is either in the circuit or named
    as absent.
    """
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [
            {"reference": "U1", "role": "reg", "search_query": "AMS1117-3.3"},
            {"reference": "C1", "role": "cin", "search_query": "capacitor", "value": "10uF"},
            {"reference": "R1", "role": "res", "search_query": "resistor", "value": "1k"},
            {"reference": "D1", "role": "led", "search_query": "LED"},
            {"reference": "J1", "role": "in", "search_query": "terminal block"},
        ],
        "netlist": [
            {"name": "VIN", "nodes": [{"reference": "J1", "pin": "1"},
                                      {"reference": "U1", "pin": "3"},
                                      {"reference": "C1", "pin": "1"}]},
            {"name": "GND", "nodes": [{"reference": "J1", "pin": "2"},
                                      {"reference": "U1", "pin": "1"},
                                      {"reference": "C1", "pin": "2"},
                                      {"reference": "D1", "pin": "K"}]},
            {"name": "3V3", "nodes": [{"reference": "U1", "pin": "2"},
                                      {"reference": "R1", "pin": "1"}]},
            {"name": "LED_NET", "nodes": [{"reference": "R1", "pin": "2"},
                                          {"reference": "D1", "pin": "A"}]},
        ],
    }
    ir, notes = agent.transcribe(spec, "ldo")

    assert set(ir.components) == {"U1", "C1", "R1", "D1", "J1"}
    assert ir.components["U1"].lib_id == "Regulator_Linear:AMS1117-3.3"
    assert ir.components["C1"].lib_id == "Device:C"
    # the request uses pins 1 and 2 of J1, so a two-way part — not whatever
    # the catalog ranked first, which on this query is a 47-way flex header
    j1 = agent.parts.load_symbols([ir.components["J1"].lib_id])
    assert len(j1[ir.components["J1"].lib_id].pins) == 2, ir.components["J1"].lib_id
    assert {n.name for n in ir.nets} == {"VIN", "GND", "3V3", "LED_NET"}
    assert agent.verify_transcription(spec, ir) == []
    assert any("no connection was inferred" in n for n in notes), notes


def test_a_reference_only_the_netlist_names_still_becomes_a_part():
    """The net list is the requirement, so a designator that appears only
    there is a part the user meant — placed, and reported as generic rather
    than dropped. The verification gate is what makes this path worth
    having: it is checkable against the words the user typed."""
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [{"reference": "R1", "role": "r", "search_query": "resistor"}],
        "netlist": [
            {"name": "A", "nodes": [{"reference": "R1", "pin": "1"}]},
            {"name": "B", "nodes": [{"reference": "R1", "pin": "2"},
                                    {"reference": "R9", "pin": "1"}]},
        ],
    }
    ir, notes = agent.transcribe(spec, "partial")
    assert set(ir.components) == {"R1", "R9"}, ir.components
    assert ir.components["R9"].lib_id == "Device:R", notes
    assert agent.verify_transcription(spec, ir) == []


def test_transcription_verification_rejects_wrong_net_extra_node_and_part():
    """Presence alone is not transcription fidelity.

    U1.3 exists in this IR, but moving it from VIN to GND changes the circuit.
    Likewise an added R2.1 and an added component are design decisions the
    transcription path is explicitly forbidden to make.
    """
    agent = object.__new__(Agent)
    spec = {
        "parts_needed": [
            {"reference": "U1", "role": "reg", "search_query": "regulator"},
            {"reference": "C1", "role": "cap", "search_query": "capacitor"},
        ],
        "netlist": [
            {"name": "VIN", "nodes": [
                {"reference": "U1", "pin": "3"},
                {"reference": "C1", "pin": "1"},
            ]},
            {"name": "GND", "nodes": [
                {"reference": "U1", "pin": "1"},
                {"reference": "C1", "pin": "2"},
            ]},
        ],
    }
    ir = CircuitIR("wrong")
    ir.add(Component("U1", "Conceptual:regulator", ""))
    ir.add(Component("C1", "Device:C", ""))
    ir.add(Component("R2", "Device:R", "1k"))
    ir.connect("VIN", ("C1", "1"))
    ir.connect("GND", ("U1", "1"), ("C1", "2"), ("U1", "3"), ("R2", "1"))

    problems = agent.verify_transcription(spec, ir)

    assert "missing connection VIN: U1.3" in problems
    assert "unexpected connection GND: U1.3" in problems
    assert "unexpected connection GND: R2.1" in problems
    assert "unexpected component R2" in problems


def test_transcription_rejects_two_terminal_part_shorted_on_one_net():
    agent = object.__new__(Agent)
    spec = {
        "parts_needed": [{"reference": "C1", "role": "gain_cap"}],
        "netlist": [{"name": "GAIN", "nodes": [
            {"reference": "C1", "pin": "1"},
            {"reference": "C1", "pin": "2"},
        ]}],
    }
    ir = CircuitIR("shorted_input")
    ir.add(Component("C1", "Device:C", "10uF"))
    ir.connect("GAIN", ("C1", "1"), ("C1", "2"))
    symbols = {"Device:C": SymbolDef("Device:C", "", [
        PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
        PinDef("2", "~", PinType.PASSIVE, 0, 0, 180, 2.54),
    ])}

    problems = agent.verify_transcription(spec, ir, symbols=symbols)

    assert "invalid two-terminal connection C1: both pins share net GAIN" in problems


def test_numbered_generic_pin_accepts_descriptive_annotation():
    assert Agent._pin_names_compatible("Wiper", "2", "Device:R_Potentiometer", "2")


def test_transcription_binds_orderable_device_suffix_and_generic_potentiometer():
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [
            {"reference": "U1", "role": "amp", "search_query": "LM386M-1"},
            {"reference": "RV1", "role": "volume", "search_query": "potentiometer"},
        ],
        "netlist": [
            {"name": "IN", "nodes": [
                {"reference": "U1", "pin": "3", "pin_name": "+IN"},
                {"reference": "RV1", "pin": "2", "pin_name": "Wiper"},
            ]},
            {"name": "GND", "nodes": [
                {"reference": "U1", "pin": "2", "pin_name": "-IN"},
                {"reference": "U1", "pin": "4", "pin_name": "GND"},
                {"reference": "RV1", "pin": "1", "pin_name": "GND"},
            ]},
            {"name": "RAW", "nodes": [
                {"reference": "RV1", "pin": "3", "pin_name": "Input"},
            ]},
        ],
    }

    ir, _notes = agent.transcribe(spec, "audio")

    assert ir.components["U1"].lib_id == "Amplifier_Audio:LM386"
    assert ir.components["RV1"].lib_id == "Device:R_Potentiometer"


def test_final_transcription_verification_allows_only_erc_infrastructure():
    agent = object.__new__(Agent)
    spec = {
        "parts_needed": [{"reference": "R1", "role": "r"}],
        "netlist": [{"name": "+5V", "nodes": [
            {"reference": "R1", "pin": "1"},
        ]}],
    }
    ir = CircuitIR("final")
    ir.add(Component("R1", "Device:R", "1k"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.connect("+5V", ("R1", "1"), ("#PWR01", "1"))

    assert agent.verify_transcription(
        spec, ir, allow_infrastructure=True
    ) == []

    ir.add(Component("C2", "Device:C", "100nF"))
    ir.connect("+5V", ("C2", "1"))
    problems = agent.verify_transcription(
        spec, ir, allow_infrastructure=True
    )
    assert "unexpected connection +5V: C2.1" in problems
    assert "unexpected component C2" in problems


def test_transcription_includes_a_listed_part_even_when_it_has_no_net_node():
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [
            {"reference": "R1", "role": "r", "search_query": "resistor", "value": "10k"},
            {"reference": "C1", "role": "c", "search_query": "capacitor", "value": "100nF"},
        ],
        "netlist": [
            {"name": "SIG", "nodes": [{"reference": "R1", "pin": "1"}]},
        ],
    }

    ir, _ = agent.transcribe(spec, "listed_part")

    assert set(ir.components) == {"R1", "C1"}
    assert ir.components["C1"].value == "100nF"
    assert agent.verify_transcription(spec, ir) == []


def test_transcription_strips_pin_annotations_from_focused_reply(agent_env):
    parts, knowledge, tmp = agent_env

    class FocusedLLM(MockLLM):
        def complete_json(self, messages, schema, **kw):
            req = set(schema.get("required", []))
            if req == {"parts", "netlist"}:
                return {
                    "parts": [
                        {"reference": "Q1", "part": "2N3904", "value": "", "package": ""},
                        {"reference": "D1", "part": "diode", "value": "", "package": ""},
                    ],
                    "netlist": [{"name": "SW", "nodes": [
                        {"reference": "Q1", "pin": "3:Collector"},
                        {"reference": "D1", "pin": "K:Cathode"},
                    ]}],
                }
            return super().complete_json(messages, schema, **kw)

    llm = FocusedLLM(spec={
        "summary": "listed connection", "power": {"rails": []},
        "parts_needed": [], "connections_intent": [], "netlist": [],
    })
    agent = Agent(llm, parts, knowledge, tmp / "pin-annotations")
    spec = agent.extract_requirements("연결 Net SW: Q1(3:Collector), D1(K:Cathode)")
    assert spec["netlist"][0]["nodes"] == [
        {"reference": "Q1", "pin": "3", "pin_name": "Collector"},
        {"reference": "D1", "pin": "K", "pin_name": "Cathode"},
    ]


def test_transcription_rejects_exact_ic_when_numbered_pin_name_conflicts():
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [{
            "reference": "U3", "role": "usb_uart", "search_query": "CH340K",
            "value": "", "package": "ESSOP-10",
        }],
        "netlist": [{"name": "GND", "nodes": [
            {"reference": "U3", "pin": "1", "pin_name": "GND"},
        ]}],
    }

    ir, notes = agent.transcribe(spec, "pin_map_conflict")

    assert ir.components["U3"].lib_id.startswith("Conceptual:")
    assert "pin 1:GND" in ir.components["U3"].binding_error
    assert any("pin 1:GND conflicts" in note and "pin 1:UD+" in note for note in notes)


def test_explicit_polarity_survives_focused_extractor_false_negative(agent_env):
    parts, knowledge, tmp_path = agent_env

    class FocusedLLM(MockLLM):
        def complete_json(self, messages, schema, **kw):
            if set(schema.get("required", [])) == {"parts", "netlist"}:
                return {
                    "parts": [
                        {"reference": "C3", "part": "capacitor", "value": "100uF",
                         "package": "SMD", "polarized": False},
                        {"reference": "C4", "part": "capacitor", "value": "100nF",
                         "package": "0805", "polarized": False},
                    ],
                    "netlist": [{"name": "VCC", "nodes": [
                        {"reference": "C3", "pin": "1"},
                        {"reference": "C4", "pin": "1"},
                    ]}],
                }
            return super().complete_json(messages, schema, **kw)

    llm = FocusedLLM(spec={
        "summary": "listed capacitors", "power": {"rails": []},
        "parts_needed": [], "connections_intent": [], "netlist": [],
    })
    agent = Agent(llm, parts, knowledge, tmp_path / "explicit-polarity")
    spec = agent.extract_requirements(
        "C3: 100uF 전해 (SMD)\nC4: 100nF 세라믹 (0805)\n"
        "Net VCC: C3(1), C4(1)"
    )

    by_ref = {part["reference"]: part for part in spec["parts_needed"]}
    assert by_ref["C3"]["polarized"] is True
    assert by_ref["C3"]["search_query"] == "polarized capacitor"
    assert by_ref["C4"]["polarized"] is False


def test_polarity_is_not_inferred_from_large_value_or_smd_package(agent_env):
    parts, knowledge, tmp_path = agent_env

    class FocusedLLM(MockLLM):
        def complete_json(self, messages, schema, **kw):
            if set(schema.get("required", [])) == {"parts", "netlist"}:
                return {
                    "parts": [{"reference": "C9", "part": "capacitor", "value": "470uF",
                               "package": "SMD", "polarized": False}],
                    "netlist": [{"name": "VCC", "nodes": [
                        {"reference": "C9", "pin": "1"},
                        {"reference": "J1", "pin": "1"},
                    ]}],
                }
            return super().complete_json(messages, schema, **kw)

    llm = FocusedLLM(spec={
        "summary": "listed capacitor", "power": {"rails": []},
        "parts_needed": [], "connections_intent": [], "netlist": [],
    })
    spec = Agent(llm, parts, knowledge, tmp_path / "no-inference").extract_requirements(
        "C9: 470uF SMD capacitor; J1 connector. Net VCC: C9(1), J1(1)"
    )

    assert next(p for p in spec["parts_needed"] if p["reference"] == "C9")["polarized"] is False


def test_explicit_nonpolarized_text_is_not_promoted():
    from circuitgen.agent import _explicit_polarized_references

    assert _explicit_polarized_references(
        "C1 is non-polarized; C2는 비극성 세라믹이다"
    ) == set()


def test_transcription_accepts_kicad_bundled_pin_number():
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [{
            "reference": "U1", "role": "mcu", "search_query": "ESP32-WROOM-32E",
            "value": "", "package": "SMD Module",
        }],
        "netlist": [{"name": "GND", "nodes": [
            {"reference": "U1", "pin": "38", "pin_name": "GND"},
        ]}],
    }

    ir, _ = agent.transcribe(spec, "stacked_pin")

    assert ir.components["U1"].lib_id == "RF_Module:ESP32-WROOM-32E"


def test_transcription_honors_a_verified_full_kicad_library_id():
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    spec = {
        "parts_needed": [{
            "reference": "U1", "role": "timer", "search_query": "Timer:NE555D",
            "value": "NE555D", "package": "SOIC-8",
        }],
        "netlist": [{"name": "OUT", "nodes": [
            {"reference": "U1", "pin": "3", "pin_name": "OUT"},
        ]}],
    }

    ir, _notes = agent.transcribe(spec, "exact_lib")

    assert ir.components["U1"].lib_id == "Timer:NE555D"
    assert ir.components["U1"].binding_error == ""


def test_pin_name_compatibility_accepts_standard_symbol_abbreviations():
    assert Agent._pin_names_compatible("Emitter", "E")
    assert Agent._pin_names_compatible("VIN", "VI")
    assert Agent._pin_names_compatible("Cathode", "K")
    assert not Agent._pin_names_compatible("GND", "UD+")


def test_connector_ground_name_expands_to_every_ground_contact():
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("usb_ground")
    ir.add(Component("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C"))
    ir.connect("GND", ("J1", "GND"))

    notes = agent.resolve_pin_names(ir)

    assert {p for r, p in ir.nets[0].nodes if r == "J1"} == {"A1", "A12", "B1", "B12"}
    assert any("4 ground pin(s)" in note for note in notes)


def test_anonymous_header_pin_tokens_become_unused_contact_numbers():
    """017 J1 stored SDA/SCL/VDD/GND as pin ids on Conn_01x04 (Pin_1..Pin_4).

    The header's job is those nets on real pads. Which number gets which
    token is not a pinout table — only that every token becomes a unused
    number and the named tokens are gone.
    """
    from circuitgen.partindex import PartIndex
    from circuitgen.topology import analyze_conduction

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("hdr-names")
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.add(Component("U2", "Sensor_Temperature:TMP100", "TMP100"))
    ir.connect("SDA", ("J1", "SDA"), ("U2", "6"))
    ir.connect("SCL", ("J1", "SCL"), ("U2", "1"))
    ir.connect("GND", ("J1", "GND"), ("U2", "2"))
    ir.connect("I2C_VDD", ("J1", "VDD"), ("U2", "4"))

    notes = agent.resolve_pin_names(ir)
    j1 = {p for n in ir.nets for r, p in n.nodes if r == "J1"}
    assert j1 == {"1", "2", "3", "4"}
    assert not (j1 & {"SDA", "SCL", "VDD", "GND"})
    by_net = {n.name: {p for r, p in n.nodes if r == "J1"} for n in ir.nets}
    assert all(len(pins) == 1 for pins in by_net.values())
    assert ("U2", "6") in next(n.nodes for n in ir.nets if n.name == "SDA")
    assert any("anonymous header contact" in n for n in notes), notes
    symbols = agent._resolve_symbols(ir)
    dead = analyze_conduction(ir, symbols).dead
    assert "J1" not in dead, dead.get("J1")

    snap = [(n.name, tuple(n.nodes)) for n in ir.nets]
    agent.resolve_pin_names(ir)
    assert [(n.name, tuple(n.nodes)) for n in ir.nets] == snap


def test_pins_the_symbol_does_not_have_are_dropped_from_nets():
    """017 C1 was Device:C with members 1–4. Pins 3 and 4 are not on the
    symbol; they stayed as unknown_pin. Same fact as the repair gate
    (G5V-1 K1.3) and ERC `unknown_pin`. A two-pin resistor's pin 3 goes
    too. Header name tokens are rewritten first, not dropped."""
    from circuitgen.erc import check_circuit
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("phantoms")
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.add(Component("R1", "Device:R", "5k"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.connect("SDA", ("C1", "1"), ("R1", "2"), ("J1", "SDA"))
    ir.connect("SCL", ("C1", "2"))
    ir.connect("I2C_VDD", ("C1", "3"), ("R1", "1"))
    ir.connect("GND", ("C1", "4"), ("R1", "3"), ("J1", "GND"))
    notes = agent.resolve_pin_names(ir)
    c1 = {p for n in ir.nets for r, p in n.nodes if r == "C1"}
    r1 = {p for n in ir.nets for r, p in n.nodes if r == "R1"}
    j1 = {p for n in ir.nets for r, p in n.nodes if r == "J1"}
    assert c1 == {"1", "2"}
    assert r1 == {"1", "2"}
    assert j1 <= {"1", "2", "3", "4"} and j1
    assert any("C1.3" in n for n in notes) and any("C1.4" in n for n in notes)
    assert any("R1.3" in n for n in notes)
    symbols = agent._resolve_symbols(ir)
    unknown = [i for i in check_circuit(ir, symbols) if i.rule == "unknown_pin"]
    assert not [i for i in unknown if i.path.startswith(("C1.", "R1."))], unknown
    assert agent.resolve_pin_names(ir) == []


def test_a_named_diode_nc_token_is_resolved_before_unknown_pins_drop():
    """nc_pins used to be dropped before fix(), so D1.A vanished."""
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("d-nc")
    ir.add(Component("D1", "Device:D", "1N4148"))
    ir.connect("NET", ("D1", "K"))
    ir.nc_pins = [("D1", "A")]
    agent.resolve_pin_names(ir)
    assert ir.nc_pins == [("D1", "2")]


def test_anonymous_header_nc_token_becomes_a_contact_number():
    """rewrite used to look at nets only, so J1.SDA as NC was dropped."""
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("j-nc")
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.connect("SDA", ("J1", "1"))
    ir.nc_pins = [("J1", "SDA")]
    agent.resolve_pin_names(ir)
    assert ir.nc_pins and ir.nc_pins[0][0] == "J1"
    assert ir.nc_pins[0][1] in {"2", "3", "4"}
    assert ir.nc_pins[0][1] != "SDA"


def test_anonymous_header_does_not_steal_a_numbered_contact():
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("hdr-mixed")
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.add(Component("U2", "Sensor_Temperature:TMP100", "TMP100"))
    ir.connect("+3V3", ("J1", "1"), ("U2", "4"))
    ir.connect("SDA", ("J1", "SDA"), ("U2", "6"))
    agent.resolve_pin_names(ir)
    plus = {p for n in ir.nets if n.name == "+3V3" for r, p in n.nodes if r == "J1"}
    sda = {p for n in ir.nets if n.name == "SDA" for r, p in n.nodes if r == "J1"}
    assert plus == {"1"}
    assert sda and sda <= {"2", "3", "4"} and "SDA" not in sda


def test_a_resistor_pin_token_is_not_an_anonymous_header_contact():
    """Device:R is nameless but not a header. SDA is not pin 1."""
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("r-token")
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("SDA", ("R1", "SDA"))
    agent.resolve_pin_names(ir)
    r1 = {p for n in ir.nets for r, p in n.nodes if r == "R1"}
    assert r1 == set()
    assert all("1" not in {p for r, p in n.nodes if r == "R1"} for n in ir.nets)


def test_named_usb_c_pin_is_not_zipped_onto_a_free_number():
    """USB-C contacts have names. A token the symbol does not have is
    not the next free pad — it is unknown and is dropped."""
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("usb-sda")
    ir.add(Component("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C"))
    ir.connect("SDA", ("J1", "SDA"))
    agent.resolve_pin_names(ir)
    j1 = [(n.name, p) for n in ir.nets for r, p in n.nodes if r == "J1"]
    assert j1 == []


def test_repair_gate_rewrites_anonymous_header_connect_ops():
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("gate-hdr")
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    kept, notes = agent._filter_ops(
        ir,
        [
            {"op": "connect", "ref": "J1", "pin": "SDA", "net": "SDA"},
            {"op": "connect", "ref": "J1", "pin": "SCL", "net": "SCL"},
            {"op": "set_nc", "ref": "J1", "pin": "FOO"},
        ],
        ["unconnected pin J1.1"],
    )
    connect = [op for op in kept if op["op"] == "connect"]
    assert [op["pin"] for op in connect] == ["1", "2"]
    assert connect[0]["net"] == "SDA" and connect[1]["net"] == "SCL"
    assert all(op["op"] != "set_nc" for op in kept)
    assert any("anonymous header contact" in n for n in notes), notes
    assert any("has no such pin" in n for n in notes), notes


def test_repair_gate_reuses_the_header_contact_already_on_that_net():
    """After SDA is pin 2, connect J1.SDA must not take pin 3."""
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("reuse")
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.add(Component("U2", "Sensor_Temperature:TMP100", "TMP100"))
    ir.connect("SDA", ("J1", "SDA"), ("U2", "6"))
    ir.connect("SCL", ("J1", "SCL"), ("U2", "1"))
    agent.resolve_pin_names(ir)
    sda_pin = next(p for n in ir.nets if n.name == "SDA" for r, p in n.nodes if r == "J1")
    kept, notes = agent._filter_ops(
        ir,
        [{"op": "connect", "ref": "J1", "pin": "SDA", "net": "SDA"}],
        ["unconnected pin J1.3"],
    )
    assert kept == [{"op": "connect", "ref": "J1", "pin": sda_pin, "net": "SDA"}]
    j1 = {p for n in ir.nets if n.name == "SDA" for r, p in n.nodes if r == "J1"}
    assert j1 == {sda_pin}


def test_two_named_tokens_on_the_same_net_share_one_header_contact():
    from circuitgen.normalize import rewrite_anonymous_header_contacts
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    lib = "Connector_Generic:Conn_01x04"
    ir = CircuitIR("same-net")
    ir.add(Component("J1", lib, "I2C"))
    ir.connect("SDA", ("J1", "SDA"), ("J1", "DATA"))
    symbols = parts.load_symbols([lib])
    rewrite_anonymous_header_contacts(ir, symbols)
    j1 = [p for r, p in ir.nets[0].nodes if r == "J1"]
    assert len(j1) == 1 and j1[0] in {"1", "2", "3", "4"}


_017_I2C_RUN = (
    Path(__file__).resolve().parent
    / "artifacts/benchmarks/sequential/ko-step-017-i2c-af-s2"
    / "006-온도센서_아이투시/run.json"
)


@pytest.mark.skipif(not _017_I2C_RUN.is_file(), reason="017 campaign artifact is local")
def test_017_named_header_pins_become_numbered_contacts():
    from circuitgen.ir_json import ir_from_json
    from circuitgen.partindex import PartIndex
    from circuitgen.topology import analyze_conduction

    run = json.loads(_017_I2C_RUN.read_text(encoding="utf-8"))
    ir = ir_from_json(run["ir"])
    before = {p for n in ir.nets for r, p in n.nodes if r == "J1"}
    assert before & {"SDA", "SCL", "VDD", "GND"}
    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.resolve_pin_names(ir)
    after = {p for n in ir.nets for r, p in n.nodes if r == "J1"}
    assert after <= {"1", "2", "3", "4"}
    assert not (after & {"SDA", "SCL", "VDD", "GND"})
    assert after == {"1", "2", "3", "4"}
    symbols = agent._resolve_symbols(ir)
    dead = analyze_conduction(ir, symbols).dead
    assert "J1" not in dead, dead.get("J1")
    c1 = {p for n in ir.nets for r, p in n.nodes if r == "C1"}
    r1 = {p for n in ir.nets for r, p in n.nodes if r == "R1"}
    assert c1 <= {"1", "2"}
    assert r1 <= {"1", "2"}


def test_transcription_uses_exact_reference_for_role_fulfilment(agent_env):
    from circuitgen.compliance import role_fulfilment

    parts, _knowledge, _tmp = agent_env
    ir = CircuitIR("refs")
    ir.add(Component("J1", "Connector_Generic:Conn_01x02", ""))
    symbols = parts.load_symbols(["Connector_Generic:Conn_01x02"])
    spec = {"parts_needed": [{
        "reference": "J1", "role": "j1", "search_query": "1x2 header", "quantity": 1,
    }]}
    total, present, missing, shortfall, unverifiable = role_fulfilment(
        spec, ir, symbols, {}
    )
    assert (total, present, missing, shortfall, unverifiable) == (1, 1, [], {}, [])


def test_transcription_preserves_part_number_bound_to_reference(agent_env):
    parts, knowledge, tmp = agent_env
    agent = Agent(MockLLM(), parts, knowledge, tmp / "named-reference")
    spec = {"parts_needed": [{
        "reference": "U1", "role": "u1", "search_query": "microcontroller",
        "value": "", "quantity": 1,
    }]}
    # Use a part present in the small test index; the rule is catalog and
    # designator based, not tied to an MCU vocabulary.
    agent._preserve_transcribed_part_numbers(
        "레귤레이터 (U1): AMS1117-3.3 (SOT-223 패키지)", spec
    )
    assert spec["parts_needed"][0]["search_query"] == "AMS1117-3"
