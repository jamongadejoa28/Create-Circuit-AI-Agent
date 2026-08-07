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
        {"ref": "SW1", "lib_id": "Switch:SW_Push", "value": "SW_Push", "footprint": "Button_Switch_SMD:SW_SPST_PTS645"},
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
    for name in ("Device", "Switch", "power"):
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
