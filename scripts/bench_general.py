#!/usr/bin/env python3
"""Run the cross-domain release suite and score functional topology.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/bench_general.py --label baseline --seed 100
"""

import argparse
import json
import time
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex
from circuitgen.topology import analyze_topology

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "data" / "eval" / "general_circuit_suite.json"


def _contract_results(required: list[str], topology: dict) -> dict[str, bool]:
    checks = {
        "amplifier_feedback": (
            topology["amplifier_total"] > 0
            and topology["amplifier_with_feedback"] == topology["amplifier_total"]
        ),
        "regulator_input_output_bypass": (
            topology["regulator_total"] > 0
            and topology["regulator_with_bypass"] == topology["regulator_total"]
        ),
    }
    return {name: checks.get(name, False) for name in required}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()

    cases = json.loads(SUITE.read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    base = LlamaClient()
    if not base.health():
        print("llama-server unreachable")
        return 1

    root_out = ROOT / "out" / "bench_general" / args.label
    root_out.mkdir(parents=True, exist_ok=True)
    results = root_out.parent / f"{args.label}.jsonl"
    parts, knowledge = PartIndex(), KnowledgeIndex()
    rows = []
    for case_index, case in enumerate(cases):
        for repeat in range(1, args.repeats + 1):
            seed = args.seed + case_index * 100 + repeat if args.seed is not None else None
            llm = LlamaClient(model=base.model, extra_payload={"seed": seed} if seed is not None else {})
            run_dir = root_out / f"{case['id']}-r{repeat}"
            agent = Agent(llm, parts, knowledge, run_dir)
            started = time.monotonic()
            res = agent.run(case["prompt"], name=case["id"])
            topology = analyze_topology(res.ir, agent._resolve_symbols(res.ir)).as_dict() if res.ir else {
                "amplifier_total": 0, "amplifier_with_feedback": 0,
                "regulator_total": 0, "regulator_with_bypass": 0, "details": [],
            }
            contracts = _contract_results(case.get("topology", []), topology)
            row = {
                "label": args.label,
                "id": case["id"],
                "domain": case["domain"],
                "repeat": repeat,
                "seed": seed,
                "stage": res.stage,
                "pipeline_ok": bool(res.pipeline and res.pipeline.ok),
                "draft_visible": bool(res.pipeline and res.pipeline.sch_path),
                "kicad_violations": len(res.pipeline.kicad_erc.violations) if res.pipeline and res.pipeline.kicad_erc else None,
                "functional_complete": res.stage not in {"functional-completeness", "functional-topology"},
                "topology": topology,
                "contracts": contracts,
                "contract_ok": all(contracts.values()),
                # ERC-clean boards shipped here with an unpowered MCU and with
                # VDD above the part's absolute maximum; a score that ignores
                # this measures drawing, not designing
                "compliance_ok": bool(res.compliance and res.compliance.ok),
                "compliance": res.compliance.as_dict() if res.compliance else None,
                "seconds": round(time.monotonic() - started, 1),
            }
            rows.append(row)
            with results.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"{case['id']} r{repeat}: stage={res.stage} pipeline={row['pipeline_ok']} "
                f"compliance={row['compliance_ok']} contracts={contracts}"
            )

    passed = sum(
        r["pipeline_ok"] and r["functional_complete"] and r["contract_ok"] and r["compliance_ok"]
        for r in rows
    )
    print(f"\nrelease score: {passed}/{len(rows)} | results: {results}")
    return 0 if passed == len(rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
