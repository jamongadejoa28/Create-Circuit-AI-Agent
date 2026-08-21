"""Deterministic IR-to-KiCad integration against the real KiCad CLI."""

from pathlib import Path

import pytest

from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.pipeline import generate
from circuitgen.symbols import KICAD_SYMBOL_DIR
from tests.fixtures.circuits import led_button_ir

pytestmark = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "pipeline"


def test_auto_placed_pipeline_is_clean(tmp_path):
    res = generate(led_button_ir(), OUT / "auto")
    assert res.errors == []
    assert res.kicad_erc is not None and res.kicad_erc.ok
    assert res.connectivity_ok, res.connectivity_msg
    assert res.svg_ok
    assert res.ok
