#!/usr/bin/env python3
"""Run the exact-answer transcription suite against the local model/KiCad."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex
if __package__:
    from .transcription_metrics import compare_expected_spec
else:
    from transcription_metrics import compare_expected_spec

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "eval" / "transcription_suite.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--suite", type=Path, default=SUITE)
    args = parser.parse_args()

    cases = json.loads(args.suite.read_text(encoding="utf-8"))
    if args.only:
        selected = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in selected]

    base = LlamaClient()
    if not base.health():
        print("llama-server unreachable")
        return 1

    out_root = ROOT / "tests" / "artifacts" / "benchmarks" / "transcription" / args.label
    out_root.mkdir(parents=True, exist_ok=True)
    results_path = out_root.parent / f"{args.label}.jsonl"
    parts, knowledge = PartIndex(), KnowledgeIndex()
    rows = []
    for case in cases:
        run_dir = out_root / case["id"]
        llm = LlamaClient(model=base.model, extra_payload={"seed": args.seed})
        agent = Agent(llm, parts, knowledge, run_dir)
        started = time.monotonic()
        result = agent.run(case["prompt"], name=case["id"])
        extraction = compare_expected_spec(case["expected"], result.spec or {})

        raw_problems: list[str] = []
        if result.spec and result.spec.get("netlist"):
            raw_ir, _ = agent.transcribe(result.spec, case["id"])
            raw_problems = agent.verify_transcription(result.spec, raw_ir)
        else:
            raw_problems = ["request was not classified as transcription"]

        pipeline = result.pipeline
        row = {
            "label": args.label,
            "id": case["id"],
            "form": case["form"],
            "seed": args.seed,
            "stage": result.stage,
            "extraction": extraction,
            "transcription_problems": raw_problems,
            "schematic_visible": bool(pipeline and pipeline.sch_path),
            "self_erc_errors": (
                sum(issue.severity == "error" for issue in pipeline.self_erc)
                if pipeline else None
            ),
            "kicad_violations": (
                len(pipeline.kicad_erc.violations)
                if pipeline and pipeline.kicad_erc else None
            ),
            "round_trip_ok": bool(pipeline and pipeline.connectivity_ok),
            "wired_ratio": (pipeline.route_metrics or {}).get("wired_ratio") if pipeline else None,
            "seconds": round(time.monotonic() - started, 1),
        }
        rows.append(row)
        with results_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"{case['id']:28} stage={result.stage:<14} "
            f"parts={extraction['parts_extracted']}/{extraction['parts_expected']} "
            f"nodes={extraction['connections_extracted']}/{extraction['connections_expected']} "
            f"net_exact={extraction['netlist_exact']} "
            f"values={extraction['values_matched']}/{extraction['values_expected']} "
            f"transcription={len(raw_problems)} problems "
            f"erc={row['kicad_violations']} roundtrip={row['round_trip_ok']}"
        )

    print(f"results: {results_path}")
    exact = all(
        row["extraction"]["netlist_exact"]
        and row["extraction"]["parts_exact"]
        and not row["transcription_problems"]
        for row in rows
    )
    return 0 if exact else 2


if __name__ == "__main__":
    raise SystemExit(main())
