#!/usr/bin/env python3
"""Export a KiCad schematic's connectivity into a candidate DatasetExample."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from circuitgen.kicad_cli import export_netlist
from tests.dataset_tools import example_from_ir, ir_from_kicad_netlist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schematic", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--mode", choices=("transcription", "design"), required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--license", dest="license_id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="circuitgen-kicad-import-") as temp:
        exported = Path(temp) / "export.net"
        result = export_netlist(args.schematic, exported)
        if result.returncode != 0 or not exported.exists():
            print(result.stderr)
            return result.returncode or 2
        ir = ir_from_kicad_netlist(exported, name=args.id)
    example = example_from_ir(
        example_id=args.id,
        prompt=args.prompt_file.read_text(encoding="utf-8"),
        mode=args.mode,
        ir=ir,
        dataset=args.dataset,
        source_project=args.source_project,
        license_id=args.license_id,
        source_revision=args.source_revision,
        validation={"parse_ok": True},
    )
    # Import is deliberately not self-approving: exact symbol/pad binding,
    # render, round trip and human review remain separate gates.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote candidate {args.id} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
