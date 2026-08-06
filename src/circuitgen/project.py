"""Minimal .kicad_pro generator.

Shipping a .kicad_pro next to a generated schematic does two things
(measured on demo files): it activates project library tables and applies
erc.rule_severities overrides. We use it to silence footprint/library
checks that are pure noise for a self-contained generated schematic —
every used symbol is embedded in lib_symbols, so "library not configured"
warnings (which otherwise fire once per symbol) carry no information.

Only the overridden severities are written; KiCad fills in defaults for
everything else.
"""

from __future__ import annotations

import json
from pathlib import Path

# Checks that are meaningless for a schematic-only, lib_symbols-embedded project.
NOISE_CHECKS = {
    "lib_symbol_issues": "ignore",
    "lib_symbol_mismatch": "ignore",
    "footprint_link_issues": "ignore",
    "footprint_filter": "ignore",
}


def write_project(sch_path: str | Path) -> Path:
    """Write NAME.kicad_pro next to NAME.kicad_sch; returns the path."""
    sch_path = Path(sch_path)
    pro_path = sch_path.with_suffix(".kicad_pro")
    pro = {
        "erc": {
            "rule_severities": dict(NOISE_CHECKS),
        },
        "meta": {
            "filename": pro_path.name,
            "version": 3,
        },
    }
    pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
    return pro_path
