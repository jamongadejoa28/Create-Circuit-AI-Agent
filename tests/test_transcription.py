import json
from pathlib import Path

from tests.benchmarks.transcription_metrics import compare_expected_spec


SUITE = Path(__file__).resolve().parent / "eval" / "transcription_suite.json"


def _as_spec(expected: dict) -> dict:
    return {
        "parts_needed": [
            {
                "reference": part["reference"],
                "search_query": part["part"],
                "value": part["value"],
                **({"polarized": part["polarized"]} if "polarized" in part else {}),
            }
            for part in expected["parts"]
        ],
        "netlist": [
            {
                "name": net["name"],
                "nodes": [
                    {"reference": reference, "pin": pin}
                    for reference, pin in net["nodes"]
                ],
            }
            for net in expected["netlist"]
        ],
    }


def test_transcription_suite_has_distinct_expected_circuits():
    cases = json.loads(SUITE.read_text(encoding="utf-8"))
    assert len(cases) >= 6
    assert len({case["id"] for case in cases}) == len(cases)
    forms = " ".join(case["form"] for case in cases).lower()
    for required in ("english", "named", "same-type", "2x3", "hierarchical", "polarized"):
        assert required in forms
    for case in cases:
        measured = compare_expected_spec(case["expected"], _as_spec(case["expected"]))
        assert measured["parts_exact"], case["id"]
        assert measured["netlist_exact"], case["id"]
        assert not measured["values_missing"], case["id"]
        assert not measured["values_wrong"], case["id"]
        assert not measured.get("polarized_wrong"), case["id"]


def test_expected_comparison_separates_net_and_value_failures():
    expected = {
        "parts": [{"reference": "R1", "part": "resistor", "value": "4.7k"}],
        "netlist": [{"name": "A", "nodes": [["R1", "1"]]}],
    }
    spec = _as_spec(expected)
    spec["parts_needed"][0]["value"] = "10k"
    spec["netlist"][0]["name"] = "B"

    measured = compare_expected_spec(expected, spec)

    assert measured["parts_exact"]
    assert not measured["netlist_exact"]
    assert measured["values_wrong"] == ["R1: expected '4.7k', got '10k'"]


def test_expected_comparison_flags_polarized_mismatch():
    expected = {
        "parts": [
            {"reference": "C1", "part": "capacitor", "value": "100uF", "polarized": True},
        ],
        "netlist": [{"name": "A", "nodes": [["C1", "1"]]}],
    }
    spec = _as_spec(expected)
    spec["parts_needed"][0]["polarized"] = False
    measured = compare_expected_spec(expected, spec)
    assert measured["polarized_wrong"] == [
        "C1: expected polarized=True, got False"
    ]
