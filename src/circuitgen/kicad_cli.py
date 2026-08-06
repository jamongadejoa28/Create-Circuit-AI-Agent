"""Adapter for the Windows kicad-cli.exe called from WSL2.

Hard requirement (measured): every filesystem path handed to the Windows
binary must be converted with `wslpath -w` first — raw /home or /mnt/c
strings fail with exit 3 ("failed to load schematic"). Converted
\\\\wsl.localhost\\... UNC paths work directly on WSL2-native files, so no
/mnt/c staging is needed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

KICAD_CLI = "/mnt/c/Program Files/KiCad/10.0/bin/kicad-cli.exe"


def wslpath_w(path: str | Path) -> str:
    """Convert a WSL path to a Windows path the KiCad binary can open."""
    out = subprocess.run(
        ["wslpath", "-w", str(path)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@dataclass
class ErcResult:
    exit_code: int
    violations: list[dict] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.violations


def run_erc(sch_path: str | Path, out_json: str | Path | None = None) -> ErcResult:
    """Run `kicad-cli sch erc` with JSON output; nonzero exit on violations."""
    sch_path = Path(sch_path)
    if out_json is None:
        out_json = sch_path.with_suffix(".erc.json")
    out_json = Path(out_json)

    proc = subprocess.run(
        [
            KICAD_CLI,
            "sch",
            "erc",
            "--format",
            "json",
            "--exit-code-violations",
            "-o",
            wslpath_w(out_json.parent) + "\\" + out_json.name,
            wslpath_w(sch_path),
        ],
        capture_output=True,
        text=True,
    )

    report: dict = {}
    violations: list[dict] = []
    if out_json.exists():
        report = json.loads(out_json.read_text())
        for sheet in report.get("sheets", []):
            violations.extend(sheet.get("violations", []))
    return ErcResult(
        exit_code=proc.returncode,
        violations=violations,
        report=report,
        stderr=proc.stderr.strip(),
    )


def export_svg(sch_path: str | Path, out_dir: str | Path) -> subprocess.CompletedProcess:
    """Render the schematic to SVG (also serves as an 'opens in KiCad' check)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            KICAD_CLI,
            "sch",
            "export",
            "svg",
            "-o",
            wslpath_w(out_dir),
            wslpath_w(sch_path),
        ],
        capture_output=True,
        text=True,
    )


def export_netlist(sch_path: str | Path, out_file: str | Path) -> subprocess.CompletedProcess:
    """Export a KiCad netlist — the oracle side of connectivity round-trip checks."""
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            KICAD_CLI,
            "sch",
            "export",
            "netlist",
            "-o",
            wslpath_w(out_file.parent) + "\\" + out_file.name,
            wslpath_w(sch_path),
        ],
        capture_output=True,
        text=True,
    )
