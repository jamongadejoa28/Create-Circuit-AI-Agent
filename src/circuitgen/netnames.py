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


_GROUND_PIN_PREFIXES = ("VSS", "GND", "AGND", "DGND", "PGND", "GNDA", "GNDD")


def is_ground_pin(name: str) -> bool:
    """True if a PIN name denotes a ground node.

    Deliberately looser than `is_ground`, which matches canonical NET names
    exactly. Vendors suffix ground pins per power domain — STM32G474 has VSSA,
    MC68HC912 has VSSX — and neither is in GROUND_NAMES. Treating those as
    positive supplies would tie a rail straight to ground.
    """
    n = name.strip().upper().replace("~", "")
    return is_ground(n) or n.startswith(_GROUND_PIN_PREFIXES)


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


def supply_voltage(name: str) -> float | None:
    """Nominal volts encoded in a rail name: +3V3 -> 3.3, +5V -> 5.0, 1V8 -> 1.8.

    Returns None when the name carries no number (VCC, VBAT), so callers can
    tell "unknown" apart from "zero".
    """
    import re

    s = name.strip().upper()
    m = re.search(r"(\d+)V(\d+)", s)          # 3V3 / 1V8 notation
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(\d+(?:\.\d+)?)\s*V", s)  # 3.3V / 12V
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def logic_rail(rails: list[str], max_volts: float = 5.5) -> str | None:
    """The digital supply among `rails`: the LOWEST-voltage supply, and only
    if it is plausibly a logic rail.

    Picking by list order instead tied a 3.3 V MCU's VDD to +12V whenever the
    spec happened to list the input rail first — an ERC-clean board that
    destroys the part on power-up.
    """
    supplies = [r for r in rails if r and not is_ground(r)]
    known = [(supply_voltage(r), r) for r in supplies]
    numbered = sorted((v, r) for v, r in known if v is not None)
    if numbered:
        volts, rail = numbered[0]
        return rail if volts <= max_volts else None
    return supplies[0] if supplies else None
