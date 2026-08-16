"""End-to-end pipeline gates (needs kicad-cli.exe — the real oracle).

These are the plan's §8.3 validation ladder as executable checks. They are
slower (each invokes the Windows kicad-cli several times) and are the
authoritative regression net for the whole generator.
"""

from pathlib import Path
from shutil import copy2

import pytest

from circuitgen.kicad_cli import KICAD_CLI, run_erc
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR
from tests.fixtures.examples import GOLDEN_PLACEMENTS, golden_led_button_ir

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "pipeline"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "golden_led_button.kicad_sch"


def test_auto_placed_pipeline_is_clean(tmp_path):
    res = generate(golden_led_button_ir(), OUT / "auto")
    assert res.errors == []
    assert res.kicad_erc is not None and res.kicad_erc.ok
    assert res.connectivity_ok, res.connectivity_msg
    assert res.svg_ok
    assert res.ok


def test_hand_placed_matches_checked_in_golden():
    res = generate(golden_led_button_ir(), OUT / "hand", placements=GOLDEN_PLACEMENTS)
    assert res.ok, res.errors
    generated = res.sch_path.read_text()
    golden = GOLDEN.read_text()
    assert generated == golden, "emitter output drifted from checked-in golden file"


def test_checked_in_golden_still_passes_kicad_erc():
    check_dir = OUT / "golden_reference"
    check_dir.mkdir(parents=True, exist_ok=True)
    checked = check_dir / GOLDEN.name
    copy2(GOLDEN, checked)
    project = GOLDEN.with_suffix(".kicad_pro")
    if project.exists():
        copy2(project, check_dir / project.name)
    result = run_erc(checked, check_dir / "golden_reference.erc.json")
    assert result.ok, [v.get("type") for v in result.violations]
