#!/usr/bin/env python3
"""KiCad 10 symbol/pin binding for the SchGen structural holdout.

Holdout rows stay candidates. This tool never writes review_status=accepted.
Human electrical review and license review remain separate gates.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from circuitgen.ir_json import ir_from_json
from circuitgen.partindex import PartIndex


def _example_index(examples_path: Path, wanted: set[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    with examples_path.open(encoding="utf-8") as stream:
        for line in stream:
            example = json.loads(line)
            if example.get("id") in wanted:
                found[example["id"]] = example
                if len(found) == len(wanted):
                    break
    return found


def bind_example(example: dict, parts: PartIndex) -> dict:
    ir_data = dict(example["expected"]["canonical_ir"] or {})
    ir_data.setdefault("name", example["id"])
    components = ir_data.get("components") or []
    missing_symbols: list[str] = []
    missing_pins: list[str] = []
    loaded = {}
    lib_ids = sorted({str(c.get("lib_id", "")) for c in components if c.get("lib_id")})
    for lib_id in lib_ids:
        try:
            loaded.update(parts.load_symbols([lib_id]))
        except KeyError:
            missing_symbols.append(lib_id)
    ir = ir_from_json(ir_data)
    for net in ir.nets:
        for ref, pin in net.nodes:
            comp = ir.components.get(ref)
            if comp is None:
                continue
            sym = loaded.get(comp.lib_id)
            if sym is None:
                continue
            try:
                sym.pin(str(pin))
            except KeyError:
                missing_pins.append(f"{ref}.{pin}@{comp.lib_id}")
    symbol_ok = not missing_symbols and not missing_pins and bool(components)
    return {
        "id": example["id"],
        "split": example.get("split"),
        "review_status": example["validation"]["review_status"],
        "symbol_binding_ok": symbol_ok,
        "component_count": len(components),
        "missing_symbols": missing_symbols,
        "missing_pins": missing_pins[:20],
        "accepted": False,
        "known_issues": list(example["validation"].get("known_issues") or []),
    }


def bind_holdout(
    holdout_path: Path,
    examples_path: Path,
    report_path: Path,
    *,
    parts: PartIndex | None = None,
) -> dict:
    holdout = [json.loads(line) for line in holdout_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    examples = _example_index(examples_path, {row["id"] for row in holdout})
    parts = parts or PartIndex()
    rows = []
    for item in holdout:
        example = examples.get(item["id"])
        if example is None:
            rows.append({
                "id": item["id"], "symbol_binding_ok": False, "accepted": False,
                "missing_symbols": ["example not found"], "missing_pins": [],
                "review_status": item.get("review_status", "candidate"),
            })
            continue
        rows.append(bind_example(example, parts))
    counts = Counter(row["symbol_binding_ok"] for row in rows)
    report = {
        "holdout": str(holdout_path),
        "examples": str(examples_path),
        "rows": len(rows),
        "symbol_binding_ok": counts[True],
        "symbol_binding_failed": counts[False],
        "accepted": 0,
        "round_trip_ok": 0,
        "render_ok": 0,
        "note": "binding success is not acceptance; render, round-trip, license and human review remain",
        "results": rows,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def physical_check(example: dict, parts: PartIndex, out_dir: Path) -> dict:
    """Emit, round-trip and render a bound IR. Never writes accepted."""
    from circuitgen.pipeline import generate

    ir_data = dict(example["expected"]["canonical_ir"] or {})
    ir_data.setdefault("name", example["id"])
    ir = ir_from_json(ir_data)
    lib_ids = sorted({c.lib_id for c in ir.components.values() if c.lib_id})
    symbols = parts.load_symbols(lib_ids) if lib_ids else {}
    result = generate(ir, out_dir, symbols=symbols, parts_index=parts)
    return {
        "netlist_round_trip_ok": bool(result.connectivity_ok),
        "render_ok": bool(result.svg_ok),
        "accepted": False,
        "review_status": "candidate",
        "errors": list(result.errors)[:8],
    }


def attach_physical(
    report: dict,
    examples: dict[str, dict],
    parts: PartIndex,
    artifact_root: Path,
) -> dict:
    round_trip = render = 0
    for row in report["results"]:
        example = examples.get(row["id"])
        if example is None or not row.get("symbol_binding_ok"):
            row.setdefault("netlist_round_trip_ok", False)
            row.setdefault("render_ok", False)
            row["accepted"] = False
            continue
        physical = physical_check(
            example, parts, artifact_root / row["id"],
        )
        physical["accepted"] = False
        physical["review_status"] = "candidate"
        row.update(physical)
        round_trip += int(physical["netlist_round_trip_ok"])
        render += int(physical["render_ok"])
    report["round_trip_ok"] = round_trip
    report["render_ok"] = render
    report["accepted"] = 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--physical",
        action="store_true",
        help="emit KiCad 10 round-trip and SVG for rows whose symbols already bind",
    )
    parser.add_argument(
        "--physical-dir",
        type=Path,
        default=None,
        help="artifact directory for physical checks (gitignored)",
    )
    args = parser.parse_args()
    report = bind_holdout(args.holdout, args.examples, args.report)
    if args.physical:
        holdout = [
            json.loads(line)
            for line in args.holdout.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        examples = _example_index(args.examples, {row["id"] for row in holdout})
        physical_dir = args.physical_dir or args.report.parent / "holdout-physical"
        attach_physical(report, examples, PartIndex(), physical_dir)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"holdout={report['rows']} binding_ok={report['symbol_binding_ok']} "
        f"failed={report['symbol_binding_failed']} "
        f"round_trip={report.get('round_trip_ok', 0)} "
        f"render={report.get('render_ok', 0)} accepted={report['accepted']}"
    )
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
