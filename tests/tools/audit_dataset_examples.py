#!/usr/bin/env python3
"""Audit DatasetExample JSON/JSONL without touching service data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.dataset_tools import audit_examples


def load_examples(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = audit_examples(load_examples(args.input))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if (
        not report["errors"]
        and not report["duplicates"]
        and not report["external_duplicates"]
        and not report["split_leakage"]
        and not report["topology_split_leakage"]
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
