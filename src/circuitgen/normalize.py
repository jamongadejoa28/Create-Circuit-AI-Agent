"""IR normalization passes that run before ERC / emission."""

from __future__ import annotations

from .ir import CircuitIR, Component, SymbolDef
from .pins import PinType

PWR_FLAG_LIB_ID = "power:PWR_FLAG"


def ensure_pwr_flags(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Add a power:PWR_FLAG to every power net lacking a power_out driver.

    KiCad ERC raises "Input Power pin not driven by any Output Power pins"
    on nets whose only power pins are power_in (all power:* supply symbols
    are power_in) — even when the topology is otherwise perfect. The
    standard fix is one PWR_FLAG (a power_out pin) per such net. Our own
    drive-sufficiency check (erc.py) fails the same way without it, so the
    two ERCs stay in agreement.

    Returns the list of added component refs (the placer must place them).
    PWR_FLAG's symbol definition must be present in `symbols`.
    """
    added: list[str] = []
    counter = 1

    def next_ref() -> str:
        nonlocal counter
        while f"#FLG{counter:02d}" in ir.components:
            counter += 1
        return f"#FLG{counter:02d}"

    for net in ir.nets:
        has_power_in = False
        has_power_out = False
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            if comp is None or comp.lib_id not in symbols:
                continue
            etype = symbols[comp.lib_id].pin(pin_no).etype
            if etype == PinType.PWRIN:
                has_power_in = True
            elif etype == PinType.PWROUT:
                has_power_out = True
        if has_power_in and not has_power_out:
            ref = next_ref()
            ir.add(Component(ref=ref, lib_id=PWR_FLAG_LIB_ID, value="PWR_FLAG"))
            net.nodes.append((ref, "1"))
            added.append(ref)
    return added
