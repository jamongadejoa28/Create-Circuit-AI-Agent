from tests.benchmarks.run_sequential_campaign import regressions


def test_regression_comparison_keeps_metrics_separate():
    old = {"draft_visible": True, "connectivity_ok": True, "visual_issues": 1,
           "wired_ratio": .8, "role_working": 8, "exact": {"netlist_exact": True}}
    new = {"draft_visible": True, "connectivity_ok": False, "visual_issues": 2,
           "wired_ratio": .7, "role_working": 7, "exact": {"netlist_exact": False}}
    found = regressions(old, new)
    assert len(found) == 5
    assert any("connectivity_ok" in item for item in found)
    assert any("exact.netlist_exact" in item for item in found)


def test_regression_detects_newly_missing_selected_part():
    found = regressions(
        {"selected_parts_missing": []},
        {"selected_parts_missing": ["MCP6001"]},
    )
    assert found == ["selected_parts_missing: newly missing MCP6001"]


def test_regression_detects_new_connector_geometry_mismatch():
    found = regressions(
        {"connector_geometry_mismatches": 0},
        {"connector_geometry_mismatches": 1},
    )
    assert found == ["connector_geometry_mismatches: 0 -> 1"]


def test_unbuilt_baseline_does_not_claim_zero_compliance_errors():
    assert regressions(
        {"draft_visible": False, "compliance_errors": 0},
        {"draft_visible": True, "compliance_errors": 4},
    ) == []
