#!/usr/bin/env python3
"""(Re)build the golden reference schematic (plan §9 Phase 1 / golden circuit 1).

Run from the repo root:

    PYTHONPATH=src .venv/bin/python scripts/make_golden.py

Writes golden/golden_led_button.kicad_sch (+ .kicad_pro) via the standard
pipeline with the hand layout from circuitgen.examples, and gates on
kicad-cli ERC + SVG render + connectivity round-trip. Exit 0 only if clean.
"""

import sys
from pathlib import Path

from circuitgen.examples import GOLDEN_PLACEMENTS, golden_led_button_ir
from circuitgen.pipeline import generate

OUT_DIR = Path(__file__).resolve().parent.parent / "golden"


def main() -> int:
    res = generate(golden_led_button_ir(), OUT_DIR, placements=GOLDEN_PLACEMENTS)
    print(f"schematic: {res.sch_path}")
    print(f"self ERC issues: {[(i.rule, i.severity) for i in res.self_erc]}")
    if res.kicad_erc:
        print(f"KiCad ERC: exit={res.kicad_erc.exit_code} violations={len(res.kicad_erc.violations)}")
        for v in res.kicad_erc.violations:
            print(f"  [{v.get('severity')}] {v.get('type')}: {v.get('description')}")
    print(f"connectivity: {res.connectivity_ok} ({res.connectivity_msg})")
    print(f"svg: {res.svg_ok}")
    for e in res.errors:
        print("ERROR:", e)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
