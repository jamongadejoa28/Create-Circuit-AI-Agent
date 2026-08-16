import json

import pytest

from tests.dataset_tools import stable_split, validate_example
from tests.schgen_adapter import (
    SchGenConversionError, cluster_projects, schgen_code_to_ir,
)
from tests.tools.convert_schgen_corpus import convert


CODE = '''
import sys
import os
sys.path.append(os.environ["PROJECT_PATH"])
from modules.kicad_sch_interface import *
add_schematic_symbol(symbol_lib="Device", symbol_name="R", pos_x=1, pos_y=2,
                     reference="R1", value="4.7k", rotation=0, mirror="None")
add_schematic_symbol(symbol_lib="Connector", symbol_name="TestPoint", pos_x=3, pos_y=4,
                     reference="TP1", value="TestPoint", rotation=0, mirror="None")
add_label(label_pos=[1, 2], label_text="SCL", label_ref="SCL_0",
          label_type="input", text_orient="left")
connect_pins("SCL_0", "1", "R1", "1")
connect_pins("R1", "2", "TP1", "TestPoint")
write_out_all_wires()
'''


def test_static_adapter_recovers_symbols_labels_and_unlabelled_nets():
    ir = schgen_code_to_ir(CODE, name="sample")
    assert ir.components["R1"].lib_id == "Device:R"
    assert ir.components["TP1"].value == "TestPoint"
    nets = {net.name: set(net.nodes) for net in ir.nets}
    assert nets["SCL"] == {("R1", "1")}
    assert any(nodes == {("R1", "2"), ("TP1", "TestPoint")} for nodes in nets.values())


def test_static_adapter_rejects_executable_control_flow_and_unknown_calls():
    with pytest.raises(SchGenConversionError, match="unsupported control"):
        schgen_code_to_ir("if True:\n  add_schematic_symbol()", name="bad")
    with pytest.raises(SchGenConversionError, match="unsupported calls"):
        schgen_code_to_ir("subprocess.run(['x'])", name="bad")


def test_project_clusters_keep_shared_code_in_one_split_group():
    rows = [
        {"project": "a", "code_sha256": "same", "topology_sha256": "one"},
        {"project": "b", "code_sha256": "same", "topology_sha256": "two"},
        {"project": "c", "code_sha256": "other", "topology_sha256": "two"},
    ]
    clusters = cluster_projects(rows).split_group_by_project
    assert clusters["a"] == clusters["b"]
    assert clusters["a"] == clusters["c"]
    assert stable_split(clusters["a"]) == stable_split(clusters["b"])


def test_project_clusters_keep_shared_prompts_out_of_holdout():
    rows = [
        {"project": "a", "prompt_sha256": "same", "code_sha256": "one",
         "topology_sha256": "top-a"},
        {"project": "b", "prompt_sha256": "same", "code_sha256": "two",
         "topology_sha256": "top-b"},
        {"project": "c", "prompt_sha256": "different", "code_sha256": "three",
         "topology_sha256": "top-c"},
    ]
    clusters = cluster_projects(rows).split_group_by_project
    assert clusters["a"] == clusters["b"]
    assert clusters["a"] != clusters["c"]


def test_corpus_converter_deduplicates_pairs_and_emits_quarantined_examples(tmp_path):
    row = {
        "messages": [
            {"role": "user", "content": "make an SCL test point"},
            {"role": "assistant", "content": CODE},
        ],
        "meta": {"module": "owner/project", "schematic": "one.kicad_sch"},
    }
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    output, report_path = tmp_path / "out.jsonl", tmp_path / "report.json"
    report = convert(source, output, report_path)
    assert report["converted_candidates"] == 1
    assert report["rejected"]["exact_pair_duplicate"] == 1
    example = json.loads(output.read_text())
    assert validate_example(example) == []
    assert example["validation"]["review_status"] == "candidate"
    assert example["validation"]["symbol_binding_ok"] is False
