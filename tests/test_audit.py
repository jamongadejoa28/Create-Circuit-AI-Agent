"""Approval audit trail, revision immutability, deterministic re-emission
(plan §12 completion criteria)."""

from pathlib import Path

import pytest

from circuitgen.audit import approve_final, is_finally_approved, load_record, sha256_file, sha256_tree
from circuitgen.emit import emit_schematic
from circuitgen.examples import GOLDEN_PLACEMENTS, golden_led_button_ir
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)


class SpecOnlyLLM:
    """Backend that answers only the spec stage — enough to reach approvals."""

    def __init__(self, spec):
        self.spec = spec

    def complete_json(self, messages, schema, **kw):
        if "parts_needed" in set(schema.get("required", [])):
            return self.spec
        raise AssertionError("should not be called past the spec stage")


SPEC = {
    "summary": "led",
    "power": {"rails": [{"name": "+5V", "voltage": "5V"}, {"name": "GND", "voltage": "0V"}]},
    "parts_needed": [{"role": "led1", "search_query": "LED"}],
    "connections_intent": ["LED on 5V"],
}


def _mini_agent(tmp_path, approve=None):
    from circuitgen.agent import Agent
    from circuitgen.knowledge import KNOWLEDGE_DIR, KnowledgeIndex, build_index as bk
    from circuitgen.partindex import PartIndex

    kdb = tmp_path / "kn.sqlite"
    bk(kdb, KNOWLEDGE_DIR)
    return Agent(SpecOnlyLLM(SPEC), PartIndex(), KnowledgeIndex(kdb), tmp_path / "run", approve_requirements=approve)


def test_requirement_rejection_blocks_generation_and_is_logged(tmp_path):
    agent = _mini_agent(tmp_path, approve=lambda spec: (False, "hajun", "wrong voltage"))
    res = agent.run("led")
    assert res.stage == "requirements-rejected"
    rec = load_record(tmp_path / "run")
    assert rec["events"][-1]["kind"] == "requirements_rejected"
    assert rec["approvals"] == []  # nothing was approved


def test_auto_approval_is_audited_and_final_approval_locks(tmp_path):
    agent = _mini_agent(tmp_path)  # no approver -> auto-approve, audited
    try:
        agent.run("led")  # dies later at synthesis (SpecOnlyLLM) — fine
    except Exception:
        pass
    rec = load_record(tmp_path / "run")
    assert rec["approvals"][0]["gate"] == "requirements"
    assert rec["approvals"][0]["approver"] == "auto"

    approve_final(tmp_path / "run", "hajun", "looks good")
    assert is_finally_approved(tmp_path / "run")

    from circuitgen.audit import RevisionLockedError

    with pytest.raises(RevisionLockedError):
        _mini_agent(tmp_path).run("led")  # same out_dir now immutable


def test_emission_is_deterministic():
    ir1 = golden_led_button_ir()
    ir2 = golden_led_button_ir()
    symbols = load_symbols(sorted({c.lib_id for c in ir1.components.values()} | {"power:PWR_FLAG"}))
    ensure_pwr_flags(ir1, symbols)
    ensure_pwr_flags(ir2, symbols)
    a = emit_schematic(ir1, symbols, GOLDEN_PLACEMENTS)
    b = emit_schematic(ir2, symbols, GOLDEN_PLACEMENTS)
    assert a == b  # byte-identical: uuid5 scheme + stable ordering (§11)


def test_audit_hashes_are_content_stable(tmp_path):
    one = tmp_path / "one.py"
    one.write_text("x = 1\n")
    first_file = sha256_file(one)
    first_tree = sha256_tree(tmp_path)
    assert first_file and first_tree
    assert sha256_file(one) == first_file
    one.write_text("x = 2\n")
    assert sha256_file(one) != first_file
    assert sha256_tree(tmp_path) != first_tree
