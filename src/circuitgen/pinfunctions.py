"""Resolve a peripheral function name to a package pin, from the datasheet.

The model writes what an engineer would say — `USART1_TX`, `FDCAN1_RX` — and
the symbol has numbers. Until now nothing could bridge that: a pin token the
symbol did not recognise was dropped or, worse, kept as a phantom pin.

The bridge is two lookups, neither of them a guess:

    USART1_TX --(data/mcu_pin_functions.json, from DS12288 Table 12)--> PA9
    PA9       --(the KiCad symbol's own pin name)-------------------->  43

The second half is free because KiCad names STM32 pins PA9. The first half is
`scripts/extract_pin_functions.py` reading the datasheet in this repository,
so the knowledge sits in a data file with a citation and no part number
appears in this module.

Package-aware by construction: the same PA9 is pin 43 on the LQFP64 symbol
and pin 31 on the LQFP48, and the symbol answers for whichever is placed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .ir import SymbolDef

DATA = Path(__file__).resolve().parents[2] / "data" / "mcu_pin_functions.json"


@lru_cache(maxsize=1)
def _devices(path: str = "") -> list[dict]:
    try:
        raw = json.loads(Path(path or DATA).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw.get("devices", [])


def device_for(lib_id: str, path: str = "") -> dict | None:
    """The recorded device whose `match` appears in this lib_id."""
    up = lib_id.upper()
    return next((d for d in _devices(path) if str(d.get("match", "")).upper() in up), None)


def pins_for_function(lib_id: str, function: str, path: str = "") -> list[str]:
    """Port pins (PA9, PB6...) the datasheet says can carry `function`."""
    device = device_for(lib_id, path)
    if device is None:
        return []
    want = function.strip().upper()
    return [
        pin for pin, entry in sorted(device.get("pins", {}).items())
        if want in {f.upper() for f in entry.get("functions", [])}
        or want in {f.upper() for f in entry.get("additional", [])}
    ]


def resolve_function_pin(
    lib_id: str, symbol: SymbolDef, function: str,
    taken: set[str] | None = None, path: str = "",
) -> tuple[str, str] | None:
    """(pin number, why) for a function name, or None when nothing is recorded.

    Among the pins that can carry the function, one that is still free is
    preferred — a peripheral has several possible pins and choosing one that
    is already wired would silently move an existing connection.
    """
    device = device_for(lib_id, path)
    if device is None:
        return None
    taken = taken or set()
    by_name = {p.name.upper(): p.number for p in symbol.pins}
    options = [
        (port, by_name[port.upper()])
        for port in pins_for_function(lib_id, function, path)
        if port.upper() in by_name
    ]
    if not options:
        return None
    free = [(port, num) for port, num in options if num not in taken]
    port, number = (free or options)[0]
    source = device.get("source", {})
    return number, (
        f"{function} -> {port} -> pin {number} "
        f"({source.get('document', 'datasheet')}, {source.get('table', 'pin table')})"
        + ("" if free else "; every pin for it was already wired")
    )
