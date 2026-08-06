"""End-to-end pipeline gates (needs kicad-cli.exe — the real oracle).

These are the plan's §8.3 validation ladder as executable checks. They are
slower (each invokes the Windows kicad-cli several times) and are the
authoritative regression net for the whole generator.
"""

from pathlib import Path

import pytest

from circuitgen.examples import GOLDEN_PLACEMENTS, golden_led_button_ir
from circuitgen.kicad_cli import KICAD_CLI, run_erc
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent.parent / "out" / "tests"


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
    golden = (Path(__file__).resolve().parent.parent / "golden" / "golden_led_button.kicad_sch").read_text()
    assert generated == golden, "emitter output drifted from checked-in golden file"


def test_checked_in_golden_still_passes_kicad_erc():
    golden = Path(__file__).resolve().parent.parent / "golden" / "golden_led_button.kicad_sch"
    result = run_erc(golden)
    assert result.ok, [v.get("type") for v in result.violations]
