"""Single source of truth for net-name classification.

Nine modules used to answer "is this net ground?" with nine different
literal sets, so the same net could be a rail to the placer and a signal
to the hierarchy emitter. Concretely: DRV8311H exposes PGND (pins 9, 25)
and AGND (16); PGND counted as ground in place/topology but not in
erc/hier_emit/agent, and GNDPWR/GNDREF only ever in erc.

The union is the correct answer — every one of these names denotes a
ground node — so it lives here and everything imports it.
"""

from __future__ import annotations

GROUND_NAMES: frozenset[str] = frozenset({
    "GND", "VSS", "0V",
    "AGND", "DGND", "PGND",      # analog / digital / power ground
    "GNDA", "GNDD", "GNDPWR", "GNDREF",
})


def is_ground(name: str) -> bool:
    """True if a net or pin name denotes a ground node (case-insensitive)."""
    return name.strip().upper() in GROUND_NAMES


def is_supply(name: str) -> bool:
    """True if a name denotes a non-ground supply rail (+3V3, +5V, VCC...).

    Deliberately narrow: a leading '+' or a VCC/VDD/VBAT-style token. Nets
    are also treated as supplies when they carry a power symbol, which the
    callers check separately — this is the name-only test.
    """
    n = name.strip().upper()
    if is_ground(n):
        return False
    return n.startswith("+") or n in {"VCC", "VDD", "VBAT", "VIN", "VBUS"}
