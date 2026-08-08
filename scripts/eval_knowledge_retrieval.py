#!/usr/bin/env python3
"""Evaluate knowledge retrieval independently from stochastic circuit synthesis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from circuitgen.knowledge import DEFAULT_DB, KnowledgeIndex

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "data" / "eval" / "knowledge_retrieval.json"


def evaluate(index: KnowledgeIndex, cases: list[dict], top_k: int) -> tuple[dict, list[dict]]:
    rows = []
    reciprocal_ranks = []
    recall_scores = []
    negative_correct = []
    for case in cases:
        hits = index.search_knowledge(case["query"], top_k, include_score=True)
        ids = [h["id"] for h in hits]
        relevant = set(case["relevant"])
        if relevant:
            found = relevant.intersection(ids)
            recall = len(found) / len(relevant)
            ranks = [ids.index(item) + 1 for item in found]
            rr = 1 / min(ranks) if ranks else 0.0
            recall_scores.append(recall)
            reciprocal_ranks.append(rr)
            passed = bool(found)
        else:
            recall = None
            rr = None
            passed = not ids
            negative_correct.append(passed)
        rows.append({
            "id": case["id"], "query": case["query"], "relevant": sorted(relevant),
            "retrieved": ids, "hit": passed, "recall": recall, "reciprocal_rank": rr,
        })
    positive = len(recall_scores)
    summary = {
        "cases": len(cases), "positive_cases": positive,
        f"hit_rate@{top_k}": sum(r["hit"] for r in rows if r["relevant"]) / positive,
        f"macro_recall@{top_k}": sum(recall_scores) / positive,
        "mrr": sum(reciprocal_ranks) / positive,
        "negative_cases": len(negative_correct),
        "no_hit_accuracy": sum(negative_correct) / len(negative_correct) if negative_correct else None,
    }
    return summary, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    summary, rows = evaluate(KnowledgeIndex(args.db), cases, args.top_k)
    result = {"summary": summary, "rows": rows}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in rows:
        status = "PASS" if row["hit"] else "FAIL"
        print(f"{status} {row['id']} expected={row['relevant']} got={row['retrieved']}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
