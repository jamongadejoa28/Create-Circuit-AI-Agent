#!/usr/bin/env python3
"""Replay saved real-model CircuitIR through the current deterministic backend.

This does not ask an LLM to generate a new answer. It isolates changes in
validation, placement, routing and KiCad emission by reusing the exact IR
stored in one or more ``run.json`` files. The resulting SVG/PNG files still
require human visual review; successful export is not image-quality approval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from circuitgen.audit import repository_revision, sha256_tree
from circuitgen.ir_json import ir_from_json
from circuitgen.partindex import PartIndex
from circuitgen.pipeline import generate

ROOT = Path(__file__).resolve().parents[2]


def discover_runs(inputs: list[Path]) -> list[Path]:
    """Return unique run.json inputs in stable path order."""
    found: set[Path] = set()
    for raw in inputs:
        path = raw.resolve()
        if path.is_dir():
            found.update(item.resolve() for item in path.rglob("run.json"))
        elif path.is_file() and path.name == "run.json":
            found.add(path)
        else:
            raise ValueError(f"not a run.json file or directory: {raw}")
    return sorted(found, key=str)


def load_saved_ir(path: Path) -> tuple[dict, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("ir"), dict):
        raise ValueError(f"{path}: no saved CircuitIR in key 'ir'")
    return payload, ir_from_json(payload["ir"])


def _safe_case_name(path: Path, index: int) -> str:
    raw = path.parent.name or f"case-{index:03d}"
    clean = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)
    return f"{index:03d}-{clean.strip('-') or 'case'}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        runs = discover_runs(args.inputs)
    except ValueError as error:
        parser.error(str(error))
    if not runs:
        parser.error("no run.json files found")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    args.output.mkdir(parents=True)

    rows = []
    parts = PartIndex()
    runtime_errors = 0
    for index, path in enumerate(runs, start=1):
        case_dir = args.output / _safe_case_name(path, index)
        try:
            payload, ir = load_saved_ir(path)
            result = generate(ir, case_dir, parts_index=parts)
            environment = payload.get("environment") or {}
            previews = [str(item) for item in result.preview_pngs]
            row = {
                "source_run": str(path),
                "source_revision": environment.get("commit"),
                "source_sha256": environment.get("source_sha256"),
                "source_model": environment.get("model"),
                "schematic": str(result.sch_path) if result.sch_path else None,
                "preview_pngs": previews,
                "visual_review_status": (
                    "not_reviewed" if previews else "preview_unavailable"
                ),
                "self_erc_errors": sum(
                    issue.severity == "error" for issue in result.self_erc
                ),
                "connectivity_ok": result.connectivity_ok,
                "visual_issues": [
                    {"rule": issue.rule, "message": issue.message}
                    for issue in result.visual_issues
                ],
                "route_metrics": result.route_metrics,
                "errors": result.errors,
            }
        except Exception as error:  # keep the remaining replay corpus visible
            runtime_errors += 1
            row = {"source_run": str(path), "runtime_error": repr(error)}
        rows.append(row)
        print(
            f"{index:03d} {path}: "
            f"runtime_error={bool(row.get('runtime_error'))} "
            f"previews={len(row.get('preview_pngs', []))}"
        )

    report = {
        "repository_revision": repository_revision(ROOT),
        "source_sha256": sha256_tree(ROOT / "src"),
        "note": "PNG export success is not visual approval; inspect preview_pngs.",
        "rows": rows,
    }
    report_path = args.output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report: {report_path}")
    return 1 if runtime_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
