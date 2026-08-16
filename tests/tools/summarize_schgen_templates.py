#!/usr/bin/env python3
"""Build a structural coverage inventory and stratified SchGen holdout.

This does not declare SchGen candidates electrically correct.  It uses every
converted candidate to measure representation diversity, while the emitted
holdout manifest retains each candidate's validation status.  Strata depend
only on graph/notation properties, never circuit names or recently failing
parts.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _size_bin(value: int) -> str:
    if value <= 1:
        return "1"
    if value <= 4:
        return "2-4"
    if value <= 8:
        return "5-8"
    if value <= 16:
        return "9-16"
    return "17+"


def structural_features(ir: dict) -> dict[str, str | bool]:
    components = ir.get("components") or []
    nets = ir.get("nets") or []
    refs = {str(component.get("ref", "")) for component in components}
    lib_ids = [str(component.get("lib_id", "")) for component in components]
    pin_tokens = [
        str(node.get("pin", ""))
        for net in nets for node in (net.get("nodes") or [])
        if str(node.get("pin", ""))
    ]
    numeric, named = any(pin.isdigit() for pin in pin_tokens), any(
        not pin.isdigit() for pin in pin_tokens
    )
    pin_notation = "mixed" if numeric and named else "numeric" if numeric else "named" if named else "none"
    degrees = [len(net.get("nodes") or []) for net in nets]
    connected_refs = {
        str(node.get("ref", "")) for net in nets for node in (net.get("nodes") or [])
    }
    repetitions = Counter(lib_ids)
    return {
        "components": _size_bin(len(components)),
        "nets": _size_bin(len(nets)),
        "max_net_degree": _size_bin(max(degrees, default=0)),
        "pin_notation": pin_notation,
        "repeated_symbol": max(repetitions.values(), default=0) >= 2,
        "connector": any(lib_id.startswith("Connector") for lib_id in lib_ids),
        "power_symbol": any(lib_id.startswith("power:") for lib_id in lib_ids),
        "has_nc": bool(ir.get("nc_pins")),
        "isolated_component": bool(refs - connected_refs),
    }


def signature(features: dict) -> str:
    return "|".join(f"{key}={str(value).lower()}" for key, value in sorted(features.items()))


def summarize(input_path: Path, report_path: Path, holdout_path: Path, per_stratum: int = 2) -> dict:
    dimension_counts: dict[str, Counter] = defaultdict(Counter)
    stratum_counts = Counter()
    by_split_stratum: dict[tuple[str, str], list[dict]] = defaultdict(list)
    validation_counts = Counter()
    rows = 0
    with input_path.open(encoding="utf-8") as stream:
        for line in stream:
            example = json.loads(line)
            rows += 1
            features = structural_features(example["expected"]["canonical_ir"])
            sig = signature(features)
            stratum_counts[sig] += 1
            validation_counts[example["validation"]["review_status"]] += 1
            for key, value in features.items():
                dimension_counts[key][str(value).lower()] += 1
            split = str(example["split"])
            if split in {"validation", "test"}:
                by_split_stratum[(split, sig)].append({
                    "id": example["id"],
                    "split": split,
                    "split_group": example["provenance"].get("split_group"),
                    "source_project": example["provenance"]["source_project"],
                    "prompt": example["input"]["prompt"],
                    "features": features,
                    "review_status": example["validation"]["review_status"],
                })
    selected = []
    for key in sorted(by_split_stratum):
        selected.extend(sorted(by_split_stratum[key], key=lambda row: row["id"])[:per_stratum])
    report = {
        "source": str(input_path),
        "examples": rows,
        "validation_status": dict(sorted(validation_counts.items())),
        "dimensions": {
            key: dict(sorted(counts.items())) for key, counts in sorted(dimension_counts.items())
        },
        "structural_strata": len(stratum_counts),
        "largest_strata": [
            {"signature": sig, "examples": count}
            for sig, count in stratum_counts.most_common(30)
        ],
        "holdout_candidates": len(selected),
        "warning": "candidate examples are not training/evaluation truth until KiCad and human validation pass",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    holdout_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with holdout_path.open("w", encoding="utf-8") as output:
        for row in selected:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--per-stratum", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(summarize(args.input, args.report, args.holdout, args.per_stratum), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
