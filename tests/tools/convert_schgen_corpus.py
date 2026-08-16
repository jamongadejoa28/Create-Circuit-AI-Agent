#!/usr/bin/env python3
"""Convert the full SchGen corpus into quarantined DatasetExample candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from circuitgen.ir_json import ir_to_json
from tests.dataset_tools import circuit_fingerprint, stable_split
from tests.schgen_adapter import (
    SchGenConversionError, cluster_projects, schgen_code_to_ir,
)


def _message(row: dict, role: str) -> str:
    return next(
        (str(item.get("content", "")) for item in row.get("messages", []) if item.get("role") == role),
        "",
    )


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            raw = json.loads(line)
            prompt, code = _message(raw, "user"), _message(raw, "assistant")
            meta = raw.get("meta") or {}
            rows.append({
                "index": index,
                "prompt": prompt,
                "code": code,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "project": str(meta.get("module") or "unknown"),
                "schematic": str(meta.get("schematic") or "unknown"),
                "style": str(meta.get("style") or "unknown"),
                "thinking_model": str(meta.get("thinking_model") or "unknown"),
            })
    return rows


def convert(input_path: Path, output_path: Path, report_path: Path) -> dict:
    rows = load_rows(input_path)
    source_revision = hashlib.sha256(input_path.read_bytes()).hexdigest()
    seen_pairs: set[str] = set()
    rejected = Counter()
    converted: list[dict] = []
    split_counts = Counter()
    for row in rows:
        pair_hash = hashlib.sha256(
            (row["prompt_sha256"] + row["code_sha256"]).encode()
        ).hexdigest()
        if pair_hash in seen_pairs:
            rejected["exact_pair_duplicate"] += 1
            continue
        seen_pairs.add(pair_hash)
        try:
            ir = schgen_code_to_ir(
                row["code"], name=f'schgen_{row["index"]}'
            )
        except SchGenConversionError as error:
            rejected[f"conversion:{error}"] += 1
            continue
        converted.append({
            **row, "pair_hash": pair_hash, "ir": ir,
            "topology_sha256": circuit_fingerprint(ir),
        })

    clusters = cluster_projects(converted)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for row in converted:
            ir = row["ir"]
            pair_hash = row["pair_hash"]
            split_group = clusters.split_group_by_project[row["project"]]
            split = stable_split(split_group)
            split_counts[split] += 1
            example = {
                "schema_version": "dataset-example-v1",
                "id": "schgen-" + pair_hash[:20],
                "split": split,
                "provenance": {
                    "dataset": "microsoft/SchGen_dataset",
                    "source_project": row["project"],
                    "license": "MIT-dataset; upstream-reference-license-review-pending",
                    "source_revision": source_revision,
                    "split_group": split_group,
                    "extraction_tool": "schgen-static-ast-adapter-v1",
                    "kicad_version": "8.x source; 10.x validation pending",
                },
                "input": {"prompt": row["prompt"], "mode": "design"},
                "requirements": {},
                "expected": {
                    "canonical_ir": ir_to_json(ir),
                    "physical_bindings": [],
                    "design_rules": [],
                    "relative_placement_constraints": [],
                    "external_representation": {
                        "kind": "schgen-python",
                        "sha256": row["code_sha256"],
                        "schematic": row["schematic"],
                        "style": row["style"],
                        "thinking_model": row["thinking_model"],
                    },
                },
                "validation": {
                    "review_status": "candidate",
                    "parse_ok": True,
                    "symbol_binding_ok": False,
                    "netlist_round_trip_ok": False,
                    "render_ok": False,
                    "known_issues": [
                        "KiCad 10 symbol binding not validated",
                        "KiCad 10 netlist round trip not validated",
                        "human electrical review pending",
                        "upstream reference license review pending",
                    ],
                },
            }
            output.write(json.dumps(example, ensure_ascii=False) + "\n")
    report = {
        "source_rows": len(rows),
        "unique_exact_pairs": len(seen_pairs),
        "converted_candidates": len(converted),
        "unique_canonical_topologies": len({row["topology_sha256"] for row in converted}),
        "split_counts": dict(sorted(split_counts.items())),
        "rejected": dict(rejected.most_common()),
        "source_sha256": source_revision,
        "output": str(output_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = convert(args.input, args.output, args.report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
