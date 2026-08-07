"""Functional-block decomposition: instantiation and deterministic merge.

Why this exists (measured, plan §7.6): a board-scale design cannot fit one
synthesis call (11,205 tokens vs the 8,192 context for a 4-axis FOC
board), and few-shot tuning of single-call synthesis is near zero-sum
across scenarios for 7B models. Splitting the design into functional
blocks makes every LLM call a trivially small sub-circuit; everything
that can be deterministic — instantiation of repeated blocks, reference
renumbering, net namespacing, rail sharing — happens here in code.

Naming rules:
  - rails (+5V/GND/...) are global everywhere.
  - a block's interface_nets are global; a repeated block writes them
    with a literal "{n}" that becomes the instance number (ENC{n}_CS →
    ENC1_CS, ENC2_CS...).
  - every other net is block-local and gets prefixed BLOCKID[n]_ so two
    blocks' internal "OUT" nets can never merge by accident.
  - references are renumbered globally per prefix (R1,R2,... across all
    instances), preserving KiCad's ref grammar.
"""

from __future__ import annotations

import re

from .ir import CircuitIR, Component

_REF_RE = re.compile(r"^(#?[A-Za-z]+)(\d+)$")


def _ref_prefix(ref: str) -> str:
    m = _REF_RE.match(ref)
    return m.group(1) if m else ref


def instantiate_blocks(
    name: str,
    plan: list[dict],
    block_irs: dict[str, CircuitIR],
    rails: list[str],
) -> tuple[CircuitIR, list[str]]:
    """Merge per-block IRs into one circuit; returns (ir, notes)."""
    merged = CircuitIR(name=name)
    notes: list[str] = []
    counters: dict[str, int] = {}
    global_names = set(rails)

    def next_ref(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    for block in plan:
        bid = block["id"]
        count = int(block.get("count", 1))
        src = block_irs.get(bid)
        if src is None:
            notes.append(f"block {bid}: no IR synthesized — skipped")
            continue
        iface_templates = [n["name"] for n in block.get("interface_nets", [])]

        for inst in range(1, count + 1):
            iface_names = {t.replace("{n}", str(inst)) for t in iface_templates}
            global_names.update(iface_names)
            ref_map: dict[str, str] = {}

            for old_ref, comp in src.components.items():
                new_ref = next_ref(_ref_prefix(old_ref))
                ref_map[old_ref] = new_ref
                merged.add(
                    Component(new_ref, comp.lib_id, comp.value, comp.footprint)
                )

            def net_name(local: str) -> str:
                resolved = local.replace("{n}", str(inst))
                if resolved in global_names:
                    return resolved
                suffix = str(inst) if count > 1 else ""
                return f"{bid}{suffix}_{resolved}"

            for net in src.nets:
                nodes = [
                    (ref_map[r], p) for r, p in net.nodes if r in ref_map
                ]
                if nodes:
                    merged.connect(net_name(net.name), *nodes)
            for r, p in src.nc_pins:
                if r in ref_map:
                    merged.nc_pins.append((ref_map[r], p))

            notes.append(
                f"block {bid}#{inst}: {len(src.components)} components as "
                f"{sorted(ref_map.values())[:6]}{'...' if len(ref_map) > 6 else ''}"
            )
    return merged, notes


def validate_plan(plan: list[dict], spec: dict) -> tuple[list[dict], list[str]]:
    """Deterministic plan sanity: every spec role must belong to a block;
    orphans are appended to the first block rather than silently dropped."""
    notes = []
    roles = {p["role"] for p in spec.get("parts_needed", [])}
    covered: set[str] = set()
    for b in plan:
        b["roles"] = [r for r in b.get("roles", []) if r in roles]
        covered.update(b["roles"])
    orphans = roles - covered
    if orphans and plan:
        plan[0]["roles"].extend(sorted(orphans))
        notes.append(f"roles {sorted(orphans)} not planned — assigned to block {plan[0]['id']}")
    seen_ids = set()
    for b in plan:
        if b["id"] in seen_ids:
            b["id"] = b["id"] + "X"
            notes.append(f"duplicate block id renamed to {b['id']}")
        seen_ids.add(b["id"])
    return plan, notes
