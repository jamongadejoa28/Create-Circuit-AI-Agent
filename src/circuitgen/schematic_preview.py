"""Rasterize KiCad SVG sheets so previews work in the editor and chat.

Cursor's image viewer and the agent Read tool show PNG/JPEG/WebP, not SVG.
The web UI already serves SVG to the browser; this module writes a sibling
``.png`` next to each ``.svg`` so local benchmark runs are visible the same
way. Missing cairosvg is non-fatal — the schematic SVG remains the artifact.
"""

from __future__ import annotations

from pathlib import Path


def rasterize_svg_file(svg_path: str | Path, *, dpi: int = 96) -> Path | None:
    """Write ``stem.png`` beside ``svg_path``. Returns the PNG path or None."""
    svg_path = Path(svg_path)
    if not svg_path.is_file() or svg_path.suffix.lower() != ".svg":
        return None
    png_path = svg_path.with_suffix(".png")
    try:
        import cairosvg
    except ImportError:
        return None
    try:
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), dpi=dpi)
    except Exception:
        return None
    return png_path if png_path.is_file() else None


def rasterize_svg_dir(svg_dir: str | Path, *, dpi: int = 96) -> list[Path]:
    """Rasterize every ``*.svg`` under ``svg_dir`` (non-recursive)."""
    svg_dir = Path(svg_dir)
    if not svg_dir.is_dir():
        return []
    written: list[Path] = []
    for svg in sorted(svg_dir.glob("*.svg")):
        png = rasterize_svg_file(svg, dpi=dpi)
        if png is not None:
            written.append(png)
    return written
