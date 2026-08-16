"""PNG siblings of KiCad SVG sheets for editor/chat preview."""

from pathlib import Path

import pytest

from circuitgen.schematic_preview import rasterize_svg_dir, rasterize_svg_file

MINIMAL_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm" viewBox="0 0 40 20">
  <rect x="1" y="1" width="38" height="18" fill="#eee" stroke="#333"/>
</svg>
"""


@pytest.fixture
def cairosvg_or_skip():
    pytest.importorskip("cairosvg")


def test_rasterize_writes_png_beside_svg(tmp_path, cairosvg_or_skip):
    svg = tmp_path / "sheet.svg"
    svg.write_text(MINIMAL_SVG, encoding="utf-8")
    png = rasterize_svg_file(svg)
    assert png is not None
    assert png == tmp_path / "sheet.png"
    assert png.is_file() and png.stat().st_size > 0


def test_rasterize_dir_converts_all_svgs(tmp_path, cairosvg_or_skip):
    for name in ("a.svg", "b.svg"):
        (tmp_path / name).write_text(MINIMAL_SVG, encoding="utf-8")
    written = rasterize_svg_dir(tmp_path)
    assert {p.name for p in written} == {"a.png", "b.png"}


def test_missing_svg_returns_none():
    assert rasterize_svg_file(Path("/no/such/file.svg")) is None
