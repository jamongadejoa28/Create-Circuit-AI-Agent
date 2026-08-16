import json
from pathlib import Path

from tests.dataset_tools import (
    adapt_schgen_row,
    audit_examples,
    circuit_fingerprint,
    example_from_ir,
    ir_from_kicad_netlist,
    stable_split,
    validate_example,
)
from tests.fixtures.examples import golden_led_button_ir
from tests.tools.check_training_readiness import readiness_report
from circuitgen.netlist import generate_netlist
from circuitgen.symbols import load_symbols


def _accepted(example_id="local-led", project="local/golden"):
    return example_from_ir(
        example_id=example_id,
        prompt="5V button LED circuit",
        mode="transcription",
        ir=golden_led_button_ir(),
        dataset="local-golden",
        source_project=project,
        license_id="MIT",
        source_revision="fixture-v1",
        validation={
            "review_status": "accepted", "parse_ok": True,
            "symbol_binding_ok": True, "netlist_round_trip_ok": True,
            "render_ok": True,
        },
    )


def test_dataset_example_schema_file_and_accepted_example_are_valid():
    schema = json.loads(
        (Path(__file__).parent / "datasets" / "dataset-example-v1.schema.json").read_text()
    )
    assert schema["properties"]["schema_version"]["const"] == "dataset-example-v1"
    assert validate_example(_accepted()) == []


def test_accepted_example_requires_provenance_and_all_structural_checks():
    example = _accepted()
    example["provenance"]["license"] = ""
    example["validation"]["netlist_round_trip_ok"] = False
    errors = validate_example(example)
    assert "provenance.license is required" in errors
    assert "accepted example requires validation.netlist_round_trip_ok=true" in errors


def test_circuit_fingerprint_ignores_component_and_net_order():
    original = _accepted()["expected"]["canonical_ir"]
    reordered = json.loads(json.dumps(original))
    reordered["components"].reverse()
    reordered["nets"].reverse()
    for net in reordered["nets"]:
        net["nodes"].reverse()
    assert circuit_fingerprint(original) == circuit_fingerprint(reordered)


def test_audit_detects_duplicate_circuits_and_repository_split_leakage():
    one = _accepted("one", "vendor/project")
    two = _accepted("two", "other/project")
    # Force an illegal split to prove leakage is named separately from the
    # stable-split validation error.
    three = _accepted("three", "vendor/project")
    three["split"] = "test" if one["split"] != "test" else "train"
    report = audit_examples([one, two, three])
    assert any(set(group) >= {"one", "two"} for group in report["duplicates"])
    assert report["split_leakage"]["vendor/project"] == sorted({one["split"], three["split"]})
    assert report["topology_split_leakage"]


def test_stable_split_is_repository_level_and_deterministic():
    assert stable_split("owner/repo") == stable_split("owner/repo")
    assert stable_split("owner/repo") in {"train", "validation", "test"}


def test_schgen_adapter_quarantines_external_code():
    schgen = adapt_schgen_row({
        "messages": [
            {"role": "user", "content": "make a test point"},
            {"role": "assistant", "content": "print('generated code')"},
        ],
        "meta": {"module": "SparkFun/Board"},
    }, revision="abc")
    assert schgen["expected"]["canonical_ir"] is None
    assert schgen["validation"]["review_status"] == "candidate"
    assert schgen["expected"]["external_representation"]["kind"] == "schgen-python"
    assert schgen["expected"]["external_representation"]["bytes"] > 0


def test_audit_reports_quarantine_reasons_and_external_duplicates():
    row = {
        "messages": [
            {"role": "user", "content": "make a test point"},
            {"role": "assistant", "content": "print('same code')"},
        ],
        "meta": {"module": "owner/project"},
    }
    one = adapt_schgen_row(row, revision="abc")
    two = adapt_schgen_row(row, revision="abc")
    two["id"] = "second-copy"
    report = audit_examples([one, two])
    assert report["quarantined"] == 2
    assert report["known_issue_counts"]["requires sandboxed code conversion"] == 2
    assert any(set(group) == {one["id"], "second-copy"} for group in report["external_duplicates"])


def test_kicad_exported_netlist_converts_to_canonical_ir(tmp_path):
    original = golden_led_button_ir()
    symbols = load_symbols(sorted({c.lib_id for c in original.components.values()}))
    path = tmp_path / "golden.net"
    path.write_text(generate_netlist(original, symbols), encoding="utf-8")
    converted = ir_from_kicad_netlist(path, name=original.name)
    assert circuit_fingerprint(converted) == circuit_fingerprint(original)


def test_training_readiness_keeps_independent_gates_and_never_uses_candidates():
    accepted = _accepted()
    candidate = _accepted("candidate", "candidate/project")
    candidate["validation"]["review_status"] = "candidate"
    report = readiness_report(
        [accepted, candidate], minimum_accepted=1,
        model_failures=4, pipeline_failures=1,
    )
    # An accepted row duplicated by a candidate is quarantined with the whole
    # duplicate group; candidate data must not inflate the usable count.
    assert report["counts"]["accepted_unique"] == 0
    assert report["gates"]["enough_human_reviewed_examples"] is False
    assert report["gates"]["model_is_dominant_failure_source"] is True
    # The accepted and candidate rows share a topology, so uniqueness remains
    # a named blocking gate rather than being hidden inside a score.
    assert report["gates"]["topology_unique"] is False
    assert report["decision"] == "not_ready"


def test_training_readiness_requires_failure_attribution():
    report = readiness_report([_accepted()], minimum_accepted=1)
    assert report["gates"]["failure_attribution_supplied"] is False
    assert report["decision"] == "not_ready"
