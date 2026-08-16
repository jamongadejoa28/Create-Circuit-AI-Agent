#!/usr/bin/env python3
"""(Re)build the golden reference schematic (plan §9 Phase 1 / golden circuit 1).

Run from the repo root:

    PYTHONPATH=src .venv/bin/python -m tests.tools.make_golden

Writes tests/fixtures/golden/golden_led_button.kicad_sch (+ .kicad_pro) via
the standard pipeline with the hand layout from tests.fixtures.examples, and gates on
kicad-cli ERC + SVG render + connectivity round-trip. Exit 0 only if clean.

This is the explicit fixture-update tool; ordinary tests copy the fixture to
tests/artifacts before asking KiCad to open it, so KiCad state never lands in
the fixture directory.
"""

import sys
from pathlib import Path
from shutil import copy2

from circuitgen.pipeline import generate
from tests.fixtures.examples import GOLDEN_PLACEMENTS, golden_led_button_ir

TEST_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = TEST_ROOT / "artifacts" / "generated" / "golden_update"
FIXTURE_DIR = TEST_ROOT / "fixtures" / "golden"


def main() -> int:
    res = generate(golden_led_button_ir(), OUT_DIR, placements=GOLDEN_PLACEMENTS)
    if res.ok and res.sch_path:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        copy2(res.sch_path, FIXTURE_DIR / res.sch_path.name)
        project = res.sch_path.with_suffix(".kicad_pro")
        if project.exists():
            copy2(project, FIXTURE_DIR / project.name)
    print(f"schematic fixture: {FIXTURE_DIR / 'golden_led_button.kicad_sch'}")
    print(f"generated artifacts: {OUT_DIR}")
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
