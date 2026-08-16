#!/usr/bin/env python3
"""Run the cross-domain release suite and MEASURE it, per circuit family.

The direction doc (§6) asks for eight separate measurements per family, not one
boolean: role/quantity fulfilment, required topology, self+KiCad ERC, netlist
round-trip, real-wire vs label-fallback ratio, visual QA, unwarranted automatic
connections, and the variance across repeats. A single ERC-shaped pass/fail
cannot say WHICH family fails or why, and optimising against it is how special
cases accumulate.

Usage:
  PYTHONPATH=src .venv/bin/python tests/benchmarks/bench_general.py --label baseline --seed 100
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.audit import repository_revision, sha256_file, sha256_tree
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex
from circuitgen.compliance import part_present
from circuitgen.evalmetrics import measure_run, summarize
from circuitgen.topology import analyze_topology

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "eval" / "general_circuit_suite.json"


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
    model = base._resolve_model()

    root_out = ROOT / "tests" / "artifacts" / "benchmarks" / "general" / args.label
    results = root_out.parent / f"{args.label}.jsonl"
    if root_out.exists() or results.exists():
        print(
            f"label {args.label!r} already exists; use a new label so runs are not mixed"
        )
        return 1
    root_out.mkdir(parents=True, exist_ok=False)
    manifest = {
        "label": args.label,
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository_revision": repository_revision(ROOT),
        "source_sha256": sha256_tree(ROOT / "src"),
        "product_data_sha256": sha256_tree(ROOT / "data", patterns=("*.json",)),
        "suite": str(SUITE.relative_to(ROOT)),
        "suite_sha256": sha256_file(SUITE),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": model,
        "seed_base": args.seed,
        "repeats": args.repeats,
        "case_ids": [case["id"] for case in cases],
    }
    (root_out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    parts, knowledge = PartIndex(), KnowledgeIndex()
    rows = []
    for case_index, case in enumerate(cases):
        for repeat in range(1, args.repeats + 1):
            seed = args.seed + case_index * 100 + repeat if args.seed is not None else None
            llm = LlamaClient(model=model, extra_payload={"seed": seed} if seed is not None else {})
            run_dir = root_out / f"{case['id']}-r{repeat}"
            agent = Agent(llm, parts, knowledge, run_dir)
            started = time.monotonic()
            res = agent.run(case["prompt"], name=case["id"])
            topology = analyze_topology(res.ir, agent._resolve_symbols(res.ir)).as_dict() if res.ir else {
                "amplifier_total": 0, "amplifier_with_feedback": 0,
                "regulator_total": 0, "regulator_with_bypass": 0, "details": [],
            }
            required = case.get("topology", [])
            contracts = _contract_results(required, topology)
            symbols = agent._resolve_symbols(res.ir) if res.ir else {}
            metrics = measure_run(
                res.spec or {}, res.ir, symbols, res.auto_connections, res.candidates
            )
            # The product assumption is that the user arrives having ALREADY
            # chosen the parts and needs the design. So the first thing to
            # measure is whether the parts they named survive into the board.
            # Checked against the case's own list, independently of the
            # prompt regex that compliance uses.
            selected = case.get("selected_parts", [])
            in_board = [
                name for name in selected
                if res.ir and any(
                    part_present(name, c.lib_id, c.value) for c in res.ir.components.values()
                )
            ]
            pr = res.pipeline
            row = {
                "label": args.label,
                "id": case["id"],
                "domain": case["domain"],
                "repeat": repeat,
                "seed": seed,
                "prompt_sha256": hashlib.sha256(case["prompt"].encode("utf-8")).hexdigest(),
                "repository_revision": manifest["repository_revision"],
                "source_sha256": manifest["source_sha256"],
                "product_data_sha256": manifest["product_data_sha256"],
                "suite_sha256": manifest["suite_sha256"],
                "model": model,
                "stage": res.stage,
                "pipeline_ok": bool(pr and pr.ok),
                "draft_visible": bool(pr and pr.sch_path),
                # -- direction doc §6: eight measurements, kept separate --
                # 3. self ERC and KiCad ERC, counted apart
                "kicad_violations": len(pr.kicad_erc.violations) if pr and pr.kicad_erc else None,
                "self_erc_errors": (
                    sum(1 for i in pr.self_erc if i.severity == "error") if pr else None
                ),
                "self_erc_warnings": (
                    sum(1 for i in pr.self_erc if i.severity == "warning") if pr else None
                ),
                # 4. netlist round-trip, no longer hidden inside pipeline_ok
                "connectivity_ok": bool(pr and pr.connectivity_ok),
                # 5. real wire vs label fallback
                "wiring": (pr.route_metrics if pr else {}) or {},
                # 6. visual QA and sheet-boundary violations
                "visual_issues": len(pr.visual_issues) if pr else None,
                # 1. requested roles/quantities, and 7. unwarranted auto-connections
                "metrics": metrics.as_dict(),
                # 2. required topology — note this list is EMPTY for six of the
                # eight cases, so contract_ok is vacuously true for them
                "topology": topology,
                "contracts": contracts,
                "contract_required": required,
                "contract_ok": all(contracts.values()),
                "selected_parts": selected,
                "selected_parts_in_board": in_board,
                "selected_parts_missing": [n for n in selected if n not in in_board],
                "unknown_to_user": case.get("unknown_to_user", []),
                "compliance_ok": bool(res.compliance and res.compliance.ok),
                "compliance": res.compliance.as_dict() if res.compliance else None,
                "seconds": round(time.monotonic() - started, 1),
            }
            rows.append(row)
            with results.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"{case['id']:18} r{repeat}: stage={res.stage:<22} "
                f"erc={row['kicad_violations']} self={row['self_erc_errors']} "
                f"conn={row['connectivity_ok']} roles={metrics.role_fulfilment} "
                f"job={metrics.role_job_done} live={metrics.parts_working}/{metrics.parts_total} "
                f"wired={row['wiring'].get('wired_ratio')} vis={row['visual_issues']} "
                f"auto={metrics.auto_connections}/{metrics.auto_no_connects}nc "
                f"parts={len(in_board)}/{len(selected)} compliance={row['compliance_ok']}"
            )

    print(f"\n--- per family (direction doc §6) ---")
    for domain, stats in summarize(rows).items():
        print(f"{domain:20} {json.dumps(stats, ensure_ascii=False)}")
    vacuous = sum(1 for r in rows if not r["contract_required"])
    print(
        f"\ncontract coverage warning: {vacuous}/{len(rows)} runs have no required "
        "topology. No aggregate pass score is computed; inspect each family and metric."
    )
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["rows"] = len(rows)
    (root_out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"results: {results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
