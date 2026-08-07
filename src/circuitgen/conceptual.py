"""Conceptual (block-level) symbols generated on demand.

When no library part fits — off-catalog modules like a Feetech servo, or
early-stage design — the circuit may still be drawn at concept level
(user decision 2026-08-07): lib_id "Conceptual:<Name>" becomes a labeled
rectangular box whose pins are whatever the IR wires to it. Electrically
they are PASSIVE pins, so ERC stays meaningful for the rest of the sheet.
"""

from __future__ import annotations

from .ir import CircuitIR, PinDef, SymbolDef
from .pins import PinType

PREFIX = "Conceptual:"


def make_conceptual_symbol(lib_id: str, pin_tokens: list[str]) -> SymbolDef:
    name = lib_id.split(":", 1)[1]
    pins: list[PinDef] = []
    tokens = list(dict.fromkeys(pin_tokens)) or ["1"]

    left = tokens[0::2]
    right = tokens[1::2]
    rows = max(len(left), len(right), 1)
    half_h = ((rows - 1) * 2.54) / 2 + 2.54
    half_w = max(7.62, 1.27 * max((len(t) for t in tokens), default=4))
    # snap to grid
    half_h = round(half_h / 1.27) * 1.27
    half_w = round(half_w / 1.27) * 1.27

    body = []
    body.append(
        f'\t\t(symbol "{name}_0_1"\n'
        f"\t\t\t(rectangle\n\t\t\t\t(start {-half_w} {-half_h})\n\t\t\t\t(end {half_w} {half_h})\n"
        f"\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type dash)\n\t\t\t\t)\n"
        f"\t\t\t\t(fill\n\t\t\t\t\t(type none)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
    )

    pin_blocks = []

    def add_pin(tok: str, idx: int, side: int) -> None:
        # side: -1 left (orientation 0, points right toward body), +1 right
        y = half_h - 2.54 * (idx + 1)
        x = -(half_w + 2.54) if side < 0 else (half_w + 2.54)
        orientation = 0 if side < 0 else 180
        pins.append(
            PinDef(number=tok, name=tok, etype=PinType.PASSIVE, x=x, y=y,
                   orientation=orientation, length=2.54)
        )
        pin_blocks.append(
            f"\t\t\t(pin passive line\n\t\t\t\t(at {x} {y} {orientation})\n"
            f"\t\t\t\t(length 2.54)\n"
            f'\t\t\t\t(name "{tok}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n'
            f'\t\t\t\t(number "{tok}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)\n'
        )

    for i, tok in enumerate(left):
        add_pin(tok, i, -1)
    for i, tok in enumerate(right):
        add_pin(tok, i, +1)

    raw = (
        f'(symbol "{name}"\n'
        "\t\t(pin_names\n\t\t\t(offset 0.508)\n\t\t)\n"
        "\t\t(exclude_from_sim yes)\n"
        "\t\t(in_bom yes)\n\t\t(on_board yes)\n"
        f'\t\t(property "Reference" "U"\n\t\t\t(at 0 {half_h + 2.54} 0)\n'
        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        f'\t\t(property "Value" "{name}"\n\t\t\t(at 0 {-(half_h + 2.54)} 0)\n'
        "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        + body[0]
        + f'\t\t(symbol "{name}_1_1"\n'
        + "".join(pin_blocks)
        + "\t\t)\n"
        + "\t)"
    )
    return SymbolDef(
        lib_id=lib_id,
        raw_sexp=raw,
        pins=pins,
        is_power=False,
        reference_prefix="U",
        properties={"Description": "conceptual block (no library part)"},
    )


def resolve_conceptual(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Synthesize box symbols for every Conceptual:* lib_id used in the IR;
    pins come from the nets / nc entries that reference the component."""
    notes = []
    for ref, comp in ir.components.items():
        if not comp.lib_id.startswith(PREFIX) or comp.lib_id in symbols:
            continue
        tokens: list[str] = []
        for net in ir.nets:
            for r, p in net.nodes:
                if r == ref:
                    tokens.append(str(p))
        for r, p in ir.nc_pins:
            if r == ref:
                tokens.append(str(p))
        symbols[comp.lib_id] = make_conceptual_symbol(comp.lib_id, tokens)
        notes.append(f"conceptual symbol generated for {comp.lib_id} with pins {tokens[:8]}")
    return notes
