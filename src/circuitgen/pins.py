"""Pin electrical types, drive levels, and the pin-conflict matrix.

Ported from SKiDL (MIT License, Copyright (c) Dave Vandenbout):
  - pin_types / pin_drives / pin_info: src/skidl/pin.py:38-179
  - conflict_matrix: src/skidl/pin.py:1020-1085
The matrix values are copied verbatim; only the container types differ.
"""

from __future__ import annotations

from enum import IntEnum


class PinType(IntEnum):
    INPUT = 1
    OUTPUT = 2
    BIDIR = 3
    TRISTATE = 4
    PASSIVE = 5
    UNSPEC = 6
    PWRIN = 7
    PWROUT = 8
    OPENCOLL = 9
    OPENEMIT = 10
    PULLUP = 11
    PULLDN = 12
    NOCONNECT = 13
    FREE = 14


class PinDrive(IntEnum):
    """Drive levels, weakest first — ordering is significant for max()."""

    NOCONNECT = 1
    NONE = 2
    PASSIVE = 3
    PULLUPDN = 4
    ONESIDE = 5
    TRISTATE = 6
    PUSHPULL = 7
    POWER = 8


# KiCad .kicad_sym electrical type token → PinType (1:1, all 12 KiCad tokens).
KICAD_PIN_TYPES: dict[str, PinType] = {
    "input": PinType.INPUT,
    "output": PinType.OUTPUT,
    "bidirectional": PinType.BIDIR,
    "tri_state": PinType.TRISTATE,
    "passive": PinType.PASSIVE,
    "free": PinType.FREE,
    "unspecified": PinType.UNSPEC,
    "power_in": PinType.PWRIN,
    "power_out": PinType.PWROUT,
    "open_collector": PinType.OPENCOLL,
    "open_emitter": PinType.OPENEMIT,
    "no_connect": PinType.NOCONNECT,
}

PIN_TYPE_TO_KICAD: dict[PinType, str] = {v: k for k, v in KICAD_PIN_TYPES.items()}


# Per-type drive capability and required receive range.
PIN_INFO: dict[PinType, dict] = {
    PinType.INPUT: {"drive": PinDrive.NONE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.PASSIVE},
    PinType.OUTPUT: {"drive": PinDrive.PUSHPULL, "max_rcv": PinDrive.PASSIVE, "min_rcv": PinDrive.NONE},
    PinType.BIDIR: {"drive": PinDrive.TRISTATE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NONE},
    PinType.TRISTATE: {"drive": PinDrive.TRISTATE, "max_rcv": PinDrive.TRISTATE, "min_rcv": PinDrive.NONE},
    PinType.PASSIVE: {"drive": PinDrive.PASSIVE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NONE},
    PinType.PULLUP: {"drive": PinDrive.PULLUPDN, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NONE},
    PinType.PULLDN: {"drive": PinDrive.PULLUPDN, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NONE},
    PinType.UNSPEC: {"drive": PinDrive.NONE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NONE},
    PinType.PWRIN: {"drive": PinDrive.NONE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.POWER},
    PinType.PWROUT: {"drive": PinDrive.POWER, "max_rcv": PinDrive.PASSIVE, "min_rcv": PinDrive.NONE},
    PinType.OPENCOLL: {"drive": PinDrive.ONESIDE, "max_rcv": PinDrive.TRISTATE, "min_rcv": PinDrive.NONE},
    PinType.OPENEMIT: {"drive": PinDrive.ONESIDE, "max_rcv": PinDrive.TRISTATE, "min_rcv": PinDrive.NONE},
    PinType.NOCONNECT: {"drive": PinDrive.NOCONNECT, "max_rcv": PinDrive.NOCONNECT, "min_rcv": PinDrive.NOCONNECT},
    PinType.FREE: {"drive": PinDrive.NONE, "max_rcv": PinDrive.POWER, "min_rcv": PinDrive.NOCONNECT},
}


OK, WARNING, ERROR = 0, 1, 2

_T = PinType

# Non-OK pairs; symmetry is applied below, unlisted pairs are OK.
_CONFLICTS: dict[tuple[PinType, PinType], tuple[int, str]] = {
    (_T.OUTPUT, _T.OUTPUT): (ERROR, ""),
    (_T.TRISTATE, _T.OUTPUT): (WARNING, ""),
    (_T.UNSPEC, _T.INPUT): (WARNING, ""),
    (_T.UNSPEC, _T.OUTPUT): (WARNING, ""),
    (_T.UNSPEC, _T.BIDIR): (WARNING, ""),
    (_T.UNSPEC, _T.TRISTATE): (WARNING, ""),
    (_T.UNSPEC, _T.PASSIVE): (WARNING, ""),
    (_T.UNSPEC, _T.PULLUP): (WARNING, ""),
    (_T.UNSPEC, _T.PULLDN): (WARNING, ""),
    (_T.UNSPEC, _T.UNSPEC): (WARNING, ""),
    (_T.PWRIN, _T.TRISTATE): (WARNING, ""),
    (_T.PWRIN, _T.UNSPEC): (WARNING, ""),
    (_T.PWROUT, _T.OUTPUT): (ERROR, ""),
    (_T.PWROUT, _T.BIDIR): (WARNING, ""),
    (_T.PWROUT, _T.TRISTATE): (ERROR, ""),
    (_T.PWROUT, _T.UNSPEC): (WARNING, ""),
    (_T.PWROUT, _T.PWROUT): (ERROR, ""),
    (_T.OPENCOLL, _T.OUTPUT): (ERROR, ""),
    (_T.OPENCOLL, _T.BIDIR): (WARNING, ""),
    (_T.OPENCOLL, _T.TRISTATE): (ERROR, ""),
    (_T.OPENCOLL, _T.UNSPEC): (WARNING, ""),
    (_T.OPENCOLL, _T.PWROUT): (ERROR, ""),
    (_T.OPENEMIT, _T.OUTPUT): (ERROR, ""),
    (_T.OPENEMIT, _T.BIDIR): (WARNING, ""),
    (_T.OPENEMIT, _T.TRISTATE): (ERROR, ""),
    (_T.OPENEMIT, _T.UNSPEC): (WARNING, ""),
    (_T.OPENEMIT, _T.PWROUT): (ERROR, ""),
    (_T.NOCONNECT, _T.INPUT): (ERROR, ""),
    (_T.NOCONNECT, _T.OUTPUT): (ERROR, ""),
    (_T.NOCONNECT, _T.BIDIR): (ERROR, ""),
    (_T.NOCONNECT, _T.TRISTATE): (ERROR, ""),
    (_T.NOCONNECT, _T.PASSIVE): (ERROR, ""),
    (_T.NOCONNECT, _T.PULLUP): (ERROR, ""),
    (_T.NOCONNECT, _T.PULLDN): (ERROR, ""),
    (_T.NOCONNECT, _T.UNSPEC): (ERROR, ""),
    (_T.NOCONNECT, _T.PWRIN): (ERROR, ""),
    (_T.NOCONNECT, _T.PWROUT): (ERROR, ""),
    (_T.NOCONNECT, _T.OPENCOLL): (ERROR, ""),
    (_T.NOCONNECT, _T.OPENEMIT): (ERROR, ""),
    (_T.NOCONNECT, _T.NOCONNECT): (ERROR, ""),
    (_T.PULLUP, _T.PULLUP): (WARNING, "Multiple pull-ups connected."),
    (_T.PULLDN, _T.PULLDN): (WARNING, "Multiple pull-downs connected."),
    (_T.PULLUP, _T.PULLDN): (ERROR, "Pull-up connected to pull-down."),
}


def pin_conflict(a: PinType, b: PinType) -> tuple[int, str]:
    """Severity (OK/WARNING/ERROR) and message for connecting two pin types."""
    hit = _CONFLICTS.get((a, b)) or _CONFLICTS.get((b, a))
    return hit if hit is not None else (OK, "")
