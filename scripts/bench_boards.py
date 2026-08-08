#!/usr/bin/env python3
"""Board-scale model benchmark over the user's testprompt.md suite.

Unlike per-scenario functional checkers, these 18 prompts are diverse
boards, so scoring is generic per run: spec/plan health, component and
conceptual counts, unknown-symbol count, KiCad ERC violations, draft
visibility, wall time. Results append to out/bench_boards/<label>.jsonl —
run once BEFORE a knowledge expansion and once after to measure its effect.

    PYTHONPATH=src .venv/bin/python scripts/bench_boards.py --label coder-base
    PYTHONPATH=src .venv/bin/python scripts/bench_boards.py --label x --only 3,7
"""

import argparse
import json
import re
import time
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex
from circuitgen.topology import analyze_topology

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "bench_boards"


def load_prompts() -> list[tuple[int, str]]:
    text = (ROOT / "testprompt.md").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"^# (\d+)\s*\n+```\n(.*?)```", text, re.M | re.S):
        out.append((int(m.group(1)), m.group(2).strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", default=None, help="comma-separated prompt numbers")
    ap.add_argument("--thinking-off", action="store_true")
    ap.add_argument("--repeats", type=int, default=1, help="runs per board")
    ap.add_argument("--seed", type=int, default=None, help="base seed; board/repeat offsets are deterministic")
    args = ap.parse_args()

    base_extra = {"chat_template_kwargs": {"enable_thinking": False}} if args.thinking_off else {}
    llm = LlamaClient(extra_payload=base_extra)
    if not llm.health():
        print("llama-server unreachable")
        return 1
    print(f"model: {llm._resolve_model()} | label: {args.label}")

    prompts = load_prompts()
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        prompts = [p for p in prompts if p[0] in wanted]

    OUT.mkdir(parents=True, exist_ok=True)
    results_path = OUT / f"{args.label}.jsonl"
    parts, knowledge = PartIndex(), KnowledgeIndex()

    rows = []
    for num, prompt in prompts:
      for repeat in range(1, args.repeats + 1):
        seed = args.seed + num * 100 + repeat if args.seed is not None else None
        extra = dict(base_extra)
        if seed is not None:
            extra["seed"] = seed
        run_llm = LlamaClient(model=llm.model, extra_payload=extra)
        suffix = "" if args.repeats == 1 else f"-r{repeat}"
        run_dir = OUT / args.label / f"board{num:02d}{suffix}"
        agent = Agent(run_llm, parts, knowledge, run_dir)
        t0 = time.monotonic()
        try:
            res = agent.run(prompt, name=f"board{num:02d}")
        except Exception as e:
            rows.append({"label": args.label, "board": num, "crashed": str(e)[:200]})
            print(f"  board{num:02d}: CRASH {e}")
            continue
        dt = time.monotonic() - t0

        ir = res.ir
        n_comp = len(ir.components) if ir else 0
        n_conceptual = (
            sum(1 for c in ir.components.values() if c.lib_id.startswith("Conceptual:"))
            if ir
            else 0
        )
        unknown = (
            sum(1 for i in (res.pipeline.self_erc if res.pipeline else []) if i.rule.startswith("unknown"))
        )
        row = {
            "label": args.label,
            "board": num,
            "repeat": repeat,
            "seed": seed,
            "ok": res.ok,
            "stage": res.stage,
            "refused": bool(res.refusal),
            "blocks": len(res.block_plan or []),
            "components": n_comp,
            "conceptual": n_conceptual,
            "unknown_symbol_issues": unknown,
            "kicad_violations": (
                len(res.pipeline.kicad_erc.violations)
                if res.pipeline and res.pipeline.kicad_erc
                else None
            ),
            "visual_issues": len(res.pipeline.visual_issues) if res.pipeline else None,
            "draft_visible": bool(res.pipeline and res.pipeline.sch_path),
            "repair_ops": len(res.repairs),
            "seconds": round(dt, 1),
        }
        if ir:
            topology = analyze_topology(ir, agent._resolve_symbols(ir))
            row["topology"] = topology.as_dict()
        requested_roles = {p.get("role") for p in (res.spec or {}).get("parts_needed", [])}
        planned_roles = {role for block in (res.block_plan or []) for role in block.get("roles", [])}
        row["required_roles"] = len(requested_roles)
        row["unplanned_roles"] = sorted(requested_roles - planned_roles) if res.block_plan else []
        run_record = run_dir / "run.json"
        if run_record.exists():
            env = json.loads(run_record.read_text(encoding="utf-8")).get("environment", {})
            row["model"] = env.get("model")
            row["knowledge_count"] = env.get("knowledge_count")
            row["knowledge_sha256"] = env.get("knowledge_sha256")
            row["source_sha256"] = env.get("source_sha256")
            row["prompt_sha256"] = env.get("prompt_sha256")
        rows.append(row)
        with results_path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"  board{num:02d}: ok={row['ok']} stage={row['stage']} comps={n_comp} "
            f"erc={row['kicad_violations']} visible={row['draft_visible']} {dt:.0f}s"
        )

    done = [r for r in rows if "crashed" not in r]
    if done:
        visible = sum(1 for r in done if r["draft_visible"])
        clean = sum(1 for r in done if r["ok"])
        avg_erc = [r["kicad_violations"] for r in done if r["kicad_violations"] is not None]
        print(f"\n== {args.label}: {len(done)} boards | visible {visible} | erc-clean {clean} | "
              f"avg violations {sum(avg_erc)/len(avg_erc):.0f}" if avg_erc else "")
    print(f"results: {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
