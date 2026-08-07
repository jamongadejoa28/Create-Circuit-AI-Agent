"""End-to-end deterministic pipeline: IR → validated .kicad_sch on disk.

Implements the plan's validation ladder (§8.3): self ERC → emission →
KiCad load/ERC → SVG render → connectivity round-trip. No LLM anywhere in
this module — Phase 4 wires an LLM in front of it; everything after the
IR stays deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .emit import emit_schematic
from .erc import check_circuit
from .geometry import Placement
from .ir import CircuitIR, SymbolDef, ValidationIssue
from .kicad_cli import ErcResult, export_netlist, export_svg, run_erc
from .netlist import compare_connectivity, generate_netlist
from .normalize import ensure_pwr_flags
from .place import heuristic_place
from .project import write_project
from .symbols import load_symbols


@dataclass
class PipelineResult:
    ok: bool
    sch_path: Path | None = None
    self_erc: list[ValidationIssue] = field(default_factory=list)
    kicad_erc: ErcResult | None = None
    connectivity_ok: bool = False
    connectivity_msg: str = ""
    svg_ok: bool = False
    errors: list[str] = field(default_factory=list)


def generate(
    ir: CircuitIR,
    out_dir: str | Path,
    placements: dict[str, Placement] | None = None,
    symbols: dict[str, SymbolDef] | None = None,
    parts_index=None,
) -> PipelineResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = PipelineResult(ok=False)

    if symbols is None:
        # Lenient: an unknown lib_id (e.g. LLM-invented) must surface as a
        # structured unknown_symbol self-ERC error, not as a crash here.
        symbols = load_symbols(
            sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}),
            strict=False,
        )

    ensure_pwr_flags(ir, symbols)

    # 1. self ERC on the IR — errors stop the pipeline before any file exists
    res.self_erc = check_circuit(ir, symbols)
    if parts_index is not None:
        from .fp_checks import check_footprints

        res.self_erc += check_footprints(ir, symbols, parts_index)
    if any(i.severity == "error" for i in res.self_erc):
        res.errors.append("self ERC errors: " + "; ".join(i.message for i in res.self_erc if i.severity == "error"))
        return res

    # 2. placement + emission
    if placements is None:
        placements = heuristic_place(ir, symbols)

    sch_path = out_dir / f"{ir.name}.kicad_sch"
    try:
        sch_text = emit_schematic(ir, symbols, placements)
    except (KeyError, ValueError) as e:
        res.errors.append(f"placement/emission error: {e}")
        return res
    sch_path.write_text(sch_text, encoding="utf-8")
    write_project(sch_path)
    res.sch_path = sch_path

    (out_dir / f"{ir.name}.net").write_text(generate_netlist(ir, symbols), encoding="utf-8")

    # 3. KiCad ERC (also proves the file loads at all)
    res.kicad_erc = run_erc(sch_path)
    if not res.kicad_erc.ok:
        res.errors.append(
            f"KiCad ERC: exit {res.kicad_erc.exit_code}, "
            + "; ".join(f"{v.get('type')}: {v.get('description')}" for v in res.kicad_erc.violations)
        )

    # 4. SVG render
    svg = export_svg(sch_path, out_dir / "svg")
    res.svg_ok = svg.returncode == 0
    if not res.svg_ok:
        res.errors.append(f"SVG export failed: {svg.stderr.strip()}")

    # 5. connectivity round-trip via kicad-cli-exported netlist
    exported = out_dir / f"{ir.name}.kicad-export.net"
    netl = export_netlist(sch_path, exported)
    if netl.returncode == 0 and exported.exists():
        res.connectivity_ok, res.connectivity_msg = compare_connectivity(ir, exported)
        if not res.connectivity_ok:
            res.errors.append(f"connectivity mismatch: {res.connectivity_msg}")
    else:
        res.errors.append(f"netlist export failed: {netl.stderr.strip()}")

    res.ok = not res.errors
    return res
