#!/usr/bin/env python3
"""Fetch small Hugging Face dataset-server samples into a research cache.

Only normalized candidate envelopes are written. Large schematic blobs,
images, generated Python and model reasoning are represented by hashes and
must be processed separately in a sandbox before an example can be accepted.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

from tests.dataset_tools import adapt_schgen_row

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "tests" / "datasets" / "sources.json"


def fetch_rows(source: dict, limit: int) -> list[dict]:
    rows: list[dict] = []
    for offset in range(0, limit, 10):
        length = min(10, limit - offset)
        query = urllib.parse.urlencode({
            "dataset": source["dataset"], "config": source.get("config", "default"),
            "split": source.get("split", "train"), "offset": offset, "length": length,
        })
        request = urllib.request.Request(
            "https://datasets-server.huggingface.co/rows?" + query,
            headers={"User-Agent": "circuitgen-dataset-audit/1"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        rows.extend(item["row"] for item in payload.get("rows", []))
        if len(payload.get("rows", [])) < length:
            break
    return rows[:limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("microsoft-schgen",), required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    sources = {entry["id"]: entry for entry in json.loads(SOURCES.read_text(encoding="utf-8"))}
    source = sources[args.source]
    examples = [
        adapt_schgen_row(row, revision=source["revision"])
        for row in fetch_rows(source, args.limit)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for example in examples:
            output.write(json.dumps(example, ensure_ascii=False) + "\n")
    print(f"wrote {len(examples)} quarantined candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
