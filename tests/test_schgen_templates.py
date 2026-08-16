import json

from tests.tools.summarize_schgen_templates import structural_features, summarize


def test_structural_features_do_not_depend_on_part_names():
    first = {"components": [
        {"ref": "U1", "lib_id": "Vendor:A"},
        {"ref": "U2", "lib_id": "Vendor:A"},
    ], "nets": [{"name": "X", "nodes": [
        {"ref": "U1", "pin": "1"}, {"ref": "U2", "pin": "DATA"},
    ]}], "nc_pins": []}
    second = {"components": [
        {"ref": "Q7", "lib_id": "Other:B"},
        {"ref": "Q8", "lib_id": "Other:B"},
    ], "nets": [{"name": "WHATEVER", "nodes": [
        {"ref": "Q7", "pin": "3"}, {"ref": "Q8", "pin": "GATE"},
    ]}], "nc_pins": []}
    assert structural_features(first) == structural_features(second)


def test_summary_stratifies_only_validation_and_test_holdout(tmp_path):
    source = tmp_path / "examples.jsonl"
    rows = []
    for index, split in enumerate(("train", "validation", "test")):
        rows.append({
            "id": f"e{index}", "split": split,
            "provenance": {"split_group": f"g{index}", "source_project": f"p{index}"},
            "input": {"prompt": f"prompt {index}"},
            "expected": {"canonical_ir": {
                "components": [{"ref": "R1", "lib_id": "Device:R"}],
                "nets": [], "nc_pins": [],
            }},
            "validation": {"review_status": "candidate"},
        })
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report_path, holdout_path = tmp_path / "report.json", tmp_path / "holdout.jsonl"
    report = summarize(source, report_path, holdout_path)
    selected = [json.loads(line) for line in holdout_path.read_text().splitlines()]
    assert report["examples"] == 3
    assert {row["split"] for row in selected} == {"validation", "test"}
    assert all(row["review_status"] == "candidate" for row in selected)


def test_holdout_binding_never_marks_accepted(tmp_path):
    from circuitgen.ir import PinDef, SymbolDef
    from circuitgen.pins import PinType
    from tests.tools.bind_schgen_holdout import bind_holdout

    class FakeParts:
        def load_symbols(self, lib_ids):
            return {
                lib_id: SymbolDef(
                    lib_id, "(symbol x)",
                    [PinDef("1", "1", PinType.PASSIVE, 0, 0, 0, 1)],
                )
                for lib_id in lib_ids
            }

    example = {
        "id": "e1", "split": "test",
        "expected": {"canonical_ir": {
            "name": "e1",
            "components": [{"ref": "R1", "lib_id": "Device:R"}],
            "nets": [{"name": "N", "nodes": [{"ref": "R1", "pin": "1"}]}],
            "nc_pins": [],
        }},
        "validation": {"review_status": "candidate", "known_issues": ["human electrical review pending"]},
    }
    holdout = tmp_path / "holdout.jsonl"
    examples = tmp_path / "examples.jsonl"
    holdout.write_text(json.dumps({"id": "e1", "review_status": "candidate"}) + "\n")
    examples.write_text(json.dumps(example) + "\n")
    report = bind_holdout(holdout, examples, tmp_path / "bind.json", parts=FakeParts())
    assert report["accepted"] == 0
    assert report["results"][0]["accepted"] is False
    assert report["results"][0]["symbol_binding_ok"] is True
    assert report["results"][0]["review_status"] == "candidate"


def test_physical_attach_never_marks_accepted(tmp_path):
    from tests.tools.bind_schgen_holdout import attach_physical
    import tests.tools.bind_schgen_holdout as bind_mod

    class FakeParts:
        pass

    report = {
        "results": [{
            "id": "e1", "symbol_binding_ok": True, "accepted": False,
            "review_status": "candidate",
        }],
        "accepted": 0,
    }
    example = {
        "id": "e1",
        "expected": {"canonical_ir": {"name": "e1", "components": [], "nets": [], "nc_pins": []}},
        "validation": {"review_status": "candidate"},
    }

    def fake_physical(_example, _parts, _out):
        return {
            "netlist_round_trip_ok": True,
            "render_ok": True,
            "accepted": True,
            "review_status": "accepted",
            "errors": [],
        }

    original = bind_mod.physical_check
    bind_mod.physical_check = fake_physical
    try:
        out = attach_physical(report, {"e1": example}, FakeParts(), tmp_path)
    finally:
        bind_mod.physical_check = original
    assert out["accepted"] == 0
    assert out["results"][0]["accepted"] is False
    assert out["results"][0]["review_status"] == "candidate"
    assert out["round_trip_ok"] == 1
    assert out["render_ok"] == 1
