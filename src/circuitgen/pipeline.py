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
    draft: bool = False  # emitted despite self-ERC errors (partial view)
    visual_issues: list = field(default_factory=list)
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

    from .conceptual import resolve_conceptual

    resolve_conceptual(ir, symbols)
    ensure_pwr_flags(ir, symbols)

    # 1. self ERC on the IR
    res.self_erc = check_circuit(ir, symbols)
    if parts_index is not None:
        from .fp_checks import check_footprints

        res.self_erc += check_footprints(ir, symbols, parts_index)
    if any(i.severity == "error" for i in res.self_erc):
        res.errors.append("self ERC errors: " + "; ".join(i.message for i in res.self_erc if i.severity == "error"))
        # DRAFT emission: an imperfect-but-visible schematic beats an
        # invisible one (user decision after the Gemini comparison — a
        # healthy CAN section was never seen because other blocks' errors
        # suppressed the file). Components whose symbols are unknown are
        # dropped from the draft only.
        known = {r for r, c in ir.components.items() if c.lib_id in symbols}
        if not known:
            return res

        def pin_ok(r: str, p) -> bool:
            try:
                symbols[ir.components[r].lib_id].pin(str(p))
                return True
            except KeyError:
                return False  # invented pin — emitter would crash on it

        draft = CircuitIR(name=ir.name)
        for r in known:
            c = ir.components[r]
            draft.add(type(c)(r, c.lib_id, c.value, c.footprint, c.group))
        for net in ir.nets:
            nodes = [(r, p) for r, p in net.nodes if r in known and pin_ok(r, p)]
            if nodes:
                draft.connect(net.name, *nodes)
        draft.nc_pins = [
            (r, p) for r, p in ir.nc_pins if r in known and pin_ok(r, p)
        ]
        ir = draft
        res.draft = True

    # 2. placement + emission
    if placements is None:
        placements = heuristic_place(ir, symbols)

    from .emit import normalize_placements
    from .visual import check_layout

    canonical_placements = normalize_placements(ir, symbols, placements)
    res.visual_issues = check_layout(ir, symbols, canonical_placements)
    if res.visual_issues:
        res.errors.extend(f"visual QA {i.rule}: {i.message}" for i in res.visual_issues)

    sch_path = out_dir / f"{ir.name}.kicad_sch"
    try:
        sch_text = emit_schematic(ir, symbols, canonical_placements)
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


def generate_hierarchical(
    ir: CircuitIR,
    out_dir: str | Path,
    name: str,
    symbols: dict[str, SymbolDef] | None = None,
    parts_index=None,
) -> PipelineResult:
    """Board-scale variant: partition by functional group into child sheets
    (dvk-mx8m-bsb style — each part complete within its own frame), emit
    root + children, then run the same oracle ladder on the ROOT (KiCad
    resolves the whole hierarchy from there)."""
    from .conceptual import resolve_conceptual
    from .hier_emit import emit_hierarchical
    from .hierarchy import partition_by_function

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = PipelineResult(ok=False)

    if symbols is None:
        symbols = load_symbols(
            sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"}),
            strict=False,
        )
    resolve_conceptual(ir, symbols)

    res.self_erc = check_circuit(ir, symbols)
    if parts_index is not None:
        from .fp_checks import check_footprints

        res.self_erc += check_footprints(ir, symbols, parts_index)
    hard = [i for i in res.self_erc if i.severity == "error"]
    if hard:
        res.errors.append("self ERC errors: " + "; ".join(i.message[:160] for i in hard[:20]))
        res.draft = True
        # draft-filter unknown symbols/pins exactly like the flat path
        known = {r for r, c in ir.components.items() if c.lib_id in symbols}
        if not known:
            return res

        def pin_ok(r, p):
            try:
                symbols[ir.components[r].lib_id].pin(str(p))
                return True
            except KeyError:
                return False

        draft = CircuitIR(name=ir.name)
        for r in known:
            c = ir.components[r]
            draft.add(type(c)(r, c.lib_id, c.value, c.footprint, c.group))
        for net in ir.nets:
            nodes = [(r, p) for r, p in net.nodes if r in known and pin_ok(r, p)]
            if nodes:
                draft.connect(net.name, *nodes)
        draft.nc_pins = [(r, p) for r, p in ir.nc_pins if r in known and pin_ok(r, p)]
        ir = draft

    partition = partition_by_function(ir)
    hier = emit_hierarchical(ir, symbols, partition, out_dir, name, parts_index)
    res.sch_path = hier["root"]
    write_project(res.sch_path)

    res.kicad_erc = run_erc(res.sch_path)
    if not res.kicad_erc.ok:
        res.errors.append(f"KiCad ERC: exit {res.kicad_erc.exit_code}, {len(res.kicad_erc.violations)} violations")

    svg = export_svg(res.sch_path, out_dir / "svg")
    res.svg_ok = svg.returncode == 0
    if not res.svg_ok:
        res.errors.append(f"SVG export failed: {svg.stderr.strip()}")

    exported = out_dir / f"{name}.kicad-export.net"
    netl = export_netlist(res.sch_path, exported)
    if netl.returncode == 0 and exported.exists():
        res.connectivity_ok, res.connectivity_msg = compare_connectivity(ir, exported)
        if not res.connectivity_ok:
            res.errors.append(f"connectivity mismatch: {res.connectivity_msg[:300]}")
    else:
        res.errors.append(f"netlist export failed: {netl.stderr.strip()}")

    res.ok = not res.errors
    return res
