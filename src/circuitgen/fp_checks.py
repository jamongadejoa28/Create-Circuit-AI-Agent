"""Footprint validation and auto-assignment (plan §8.2 completion).

Until now Component.footprint was free text — models wrote lib-less or
invented names and nothing caught it (KiCad's own footprint checks are
deliberately silenced in our .kicad_pro). With the official
kicad-footprints index these become deterministic:

- validation: the footprint must exist, and every pin NUMBER of the
  symbol must exist among the footprint's pad numbers (count comparison
  is not enough — KiCad connects pads by number).
- assignment: empty/unknown footprints are filled from, in order, the
  symbol's own default Footprint property, then its ki_fp_filters
  matched against the index.

Both no-op when the index carries no footprints (not built with the
kicad-footprints clone present).
"""

from __future__ import annotations

from .ir import CircuitIR, SymbolDef, ValidationIssue
from .partindex import PartIndex


def _required_pins(sym: SymbolDef) -> set[str]:
    return {p.number for p in sym.pins}


def check_footprints(
    ir: CircuitIR, symbols: dict[str, SymbolDef], parts: PartIndex
) -> list[ValidationIssue]:
    if not parts.has_footprints():
        return []
    issues = []
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#") or not comp.footprint:
            continue
        if comp.lib_id.startswith("Conceptual:"):
            continue  # concept boxes have no physical package by definition
        pads = parts.footprint_pads(comp.footprint)
        if pads is None:
            issues.append(
                ValidationIssue(
                    "circuitgen-erc", "footprint_unknown", "error", ref,
                    f"{ref}: footprint {comp.footprint!r} does not exist in the footprint library",
                )
            )
            continue
        missing = _required_pins(sym) - pads
        if missing:
            issues.append(
                ValidationIssue(
                    "circuitgen-erc", "footprint_pin_mismatch", "error", ref,
                    f"{ref}: footprint {comp.footprint} has no pads for pins {sorted(missing)} of {comp.lib_id}",
                )
            )
    return issues


def assign_footprints(
    ir: CircuitIR, symbols: dict[str, SymbolDef], parts: PartIndex
) -> list[str]:
    """Fill or repair footprints deterministically; returns notes."""
    if not parts.has_footprints():
        return []
    notes = []
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        if comp.lib_id.startswith("Conceptual:"):
            comp.footprint = ""  # concept boxes carry no package
            continue
        if comp.footprint and parts.footprint_pads(comp.footprint) is not None:
            continue  # valid as-is
        required = _required_pins(sym)

        default_fp = sym.properties.get("Footprint", "")
        if default_fp:
            pads = parts.footprint_pads(default_fp)
            if pads is not None and required <= pads:
                if comp.footprint != default_fp:
                    notes.append(f"{ref}: footprint <- {default_fp} (symbol default)")
                    comp.footprint = default_fp
                continue

        filters = (sym.properties.get("ki_fp_filters", "") or "").split()
        # A few generic KiCad symbols intentionally carry no filters.  Give
        # them conservative, deterministic package families rather than
        # preserving a model-invented footprint forever.
        if not filters:
            filters = {
                "Switch:SW_Push": ["SW_SPST*"],
                "Device:Fuse": ["*Fuse*"],
                "Device:D_TVS": ["D_*"],
            }.get(comp.lib_id, [])
        if filters:
            matches = parts.match_footprints(filters, required, limit=1)
            if matches:
                notes.append(f"{ref}: footprint <- {matches[0]} (fp_filters {filters})")
                comp.footprint = matches[0]
                continue

        if comp.footprint:
            notes.append(f"{ref}: footprint {comp.footprint!r} unknown and no valid replacement found")
    return notes
