#!/usr/bin/env python3
"""Run campaign cases 1..N and report per-case regressions against baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.audit import repository_revision, sha256_tree
from circuitgen.compliance import part_present
from circuitgen.ir_json import ir_to_json
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex
from tests.benchmarks.transcription_metrics import compare_expected_spec

ROOT = Path(__file__).resolve().parents[2]


def _snapshot(case: dict, result, seconds: float) -> dict:
    pipeline = result.pipeline
    compliance = result.compliance.as_dict() if result.compliance else {}
    ir_json = ir_to_json(result.ir) if result.ir else None
    selected = case.get("selected_parts", [])
    selected_in_board = [
        requested for requested in selected
        if result.ir and any(
            (
                requested.casefold() == component.lib_id.casefold()
                if ":" in requested else
                part_present(requested, component.lib_id, component.value)
            )
            for component in result.ir.components.values()
        )
    ]
    row = {
        "sequence": case["sequence"], "id": case["id"], "oracle": case["oracle"],
        "stage": result.stage, "draft_visible": bool(pipeline and pipeline.sch_path),
        "connectivity_ok": bool(pipeline and pipeline.connectivity_ok),
        "self_erc_errors": (
            sum(issue.severity == "error" for issue in pipeline.self_erc)
            if pipeline else None
        ),
        "self_erc_warnings": (
            sum(issue.severity == "warning" for issue in pipeline.self_erc)
            if pipeline else None
        ),
        "kicad_violations": len(pipeline.kicad_erc.violations) if pipeline and pipeline.kicad_erc else None,
        "visual_issues": len(pipeline.visual_issues) if pipeline else None,
        "wired_ratio": (pipeline.route_metrics or {}).get("wired_ratio") if pipeline else None,
        "compliance_errors": (
            sum(i.get("severity") == "error" for i in compliance.get("issues", []))
            if result.compliance else None
        ),
        "role_working": compliance.get("role_working"), "role_total": compliance.get("role_total"),
        "selected_parts": selected,
        "selected_parts_in_board": selected_in_board,
        "selected_parts_missing": [part for part in selected if part not in selected_in_board],
        "connector_geometry": compliance.get("connector_geometry") or [],
        "connector_geometry_mismatches": sum(
            1 for item in (compliance.get("connector_geometry") or [])
            if item.get("match") is False
        ) if result.compliance else None,
        "ir_sha256": hashlib.sha256(json.dumps(ir_json, sort_keys=True).encode()).hexdigest() if ir_json else None,
        "seconds": round(seconds, 1),
    }
    if case["oracle"] == "exact":
        row["exact"] = compare_expected_spec(case["expected"], result.spec or {})
    return row


def regressions(old: dict, new: dict) -> list[str]:
    problems = []
    for key in ("draft_visible", "connectivity_ok"):
        if old.get(key) is True and new.get(key) is not True:
            problems.append(f"{key}: true -> {new.get(key)!r}")
    for key in ("visual_issues",):
        if old.get(key) is not None and new.get(key) is not None and new[key] > old[key]:
            problems.append(f"{key}: {old[key]} -> {new[key]}")
    # ERC counts are recorded on the row but do not gate the runner. A
    # geometry fix that makes a dead board honest can raise ERC while
    # product metrics (roles, selected parts, contacts) are the gate.
    # Old reports created before compliance existed encoded an unbuilt board
    # as zero errors. A metric is comparable only when both runs produced a
    # draft on which compliance could actually run.
    if (
        old.get("draft_visible") is True and new.get("draft_visible") is True
        and old.get("compliance_errors") is not None
        and new.get("compliance_errors") is not None
        and new["compliance_errors"] > old["compliance_errors"]
    ):
        problems.append(
            f"compliance_errors: {old['compliance_errors']} -> {new['compliance_errors']}"
        )
    if old.get("wired_ratio") is not None and new.get("wired_ratio") is not None and new["wired_ratio"] < old["wired_ratio"]:
        problems.append(f"wired_ratio: {old['wired_ratio']} -> {new['wired_ratio']}")
    if old.get("role_working") is not None and new.get("role_working") is not None and new["role_working"] < old["role_working"]:
        problems.append(f"role_working: {old['role_working']} -> {new['role_working']}")
    if (
        old.get("connector_geometry_mismatches") is not None
        and new.get("connector_geometry_mismatches") is not None
        and new["connector_geometry_mismatches"] > old["connector_geometry_mismatches"]
    ):
        problems.append(
            f"connector_geometry_mismatches: {old['connector_geometry_mismatches']} "
            f"-> {new['connector_geometry_mismatches']}"
        )
    old_missing = set(old.get("selected_parts_missing", []))
    new_missing = set(new.get("selected_parts_missing", []))
    for part in sorted(new_missing - old_missing):
        problems.append(f"selected_parts_missing: newly missing {part}")
    old_exact, new_exact = old.get("exact", {}), new.get("exact", {})
    for key in ("parts_exact", "netlist_exact"):
        if old_exact.get(key) is True and new_exact.get(key) is not True:
            problems.append(f"exact.{key}: true -> {new_exact.get(key)!r}")
    if old_exact.get("polarized_wrong") == [] and new_exact.get("polarized_wrong"):
        problems.append(
            "exact.polarized_wrong: " + ", ".join(new_exact["polarized_wrong"])
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    campaign = json.loads(args.campaign.read_text(encoding="utf-8"))
    if not 1 <= args.step <= len(campaign):
        parser.error(f"--step must be within 1..{len(campaign)}")
    cases = campaign[:args.step]
    prior = {}
    if args.baseline:
        prior = {row["id"]: row for row in json.loads(args.baseline.read_text(encoding="utf-8"))["rows"]}
    base = LlamaClient()
    if not base.health():
        print("llama-server unreachable")
        return 1
    out_dir = ROOT / "tests/artifacts/benchmarks/sequential" / args.label
    out_dir.mkdir(parents=True, exist_ok=False)
    parts, knowledge, rows = PartIndex(), KnowledgeIndex(), []
    all_regressions = {}
    for case in cases:
        started = time.monotonic()
        result = Agent(
            LlamaClient(model=base.model, extra_payload={"seed": args.seed}),
            parts, knowledge, out_dir / f'{case["sequence"]:03d}-{case["id"].replace(":", "-")}',
        ).run(case["prompt"], name=case["id"].replace(":", "_"))
        row = _snapshot(case, result, time.monotonic() - started)
        rows.append(row)
        if case["id"] in prior:
            found = regressions(prior[case["id"]], row)
            if found:
                all_regressions[case["id"]] = found
        print(f'{case["sequence"]:03d} {case["id"]} stage={row["stage"]} regressions={len(all_regressions.get(case["id"], []))}')
    report = {
        "label": args.label, "step": args.step, "seed": args.seed,
        "repository_revision": repository_revision(ROOT),
        "source_sha256": sha256_tree(ROOT / "src"),
        "campaign_sha256": hashlib.sha256(args.campaign.read_bytes()).hexdigest(),
        "baseline": str(args.baseline) if args.baseline else None,
        "regressions": all_regressions, "rows": rows,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report: {out_dir / 'report.json'}")
    return 3 if all_regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
