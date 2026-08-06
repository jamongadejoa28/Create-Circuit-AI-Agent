"""Pin absolute-position math.

The single most failure-prone piece of the pipeline: a label/stub that
misses the true pin position by any amount silently becomes an
"unconnected pin" in KiCad ERC. Connectivity in .kicad_sch is pure
coordinate matching, so this transform must be exact.

Transform derived empirically from six wire↔pin triples in the KiCad 10
demo pic_programmer.kicad_sch, covering rotations 0/90/180/270 and both
mirrors (see tests/test_geometry.py):

  1. mirror in symbol space        (x: py→−py, y: px→−px)
  2. flip symbol Y-up → sheet Y-down  (py→−py)
  3. rotate by the placement rotation with the standard matrix in sheet axes
  4. translate to the anchor (X, Y)

Spot checks: rot 0 → (X+px, Y−py); rot 90 → (X+py, Y+px);
rot 180 → (X−px, Y+py); rot 270 → (X−py, Y−px).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .ir import PinDef

GRID = 1.27  # KiCad schematic base grid in mm (50 mil); pins usually on 2.54


@dataclass(frozen=True)
class Placement:
    x: float
    y: float
    rotation: int = 0  # degrees, 0/90/180/270
    mirror: str | None = None  # None | "x" | "y"


def _round(v: float) -> float:
    """Kill float noise so emitted coordinates compare exactly."""
    return round(v + 0.0, 4)


def _sym_to_sheet_vec(
    dx: float, dy: float, rotation: int, mirror: str | None
) -> tuple[float, float]:
    """Map a symbol-space vector to a sheet-space vector for a placement."""
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx

    dy = -dy  # symbol space is Y-up, sheet space is Y-down

    theta = math.radians(rotation % 360)
    c, s = round(math.cos(theta)), round(math.sin(theta))
    return dx * c - dy * s, dx * s + dy * c


def pin_absolute_position(place: Placement, pin: PinDef) -> tuple[float, float]:
    """Sheet-space coordinate of a library pin for a placed symbol."""
    rx, ry = _sym_to_sheet_vec(pin.x, pin.y, place.rotation, place.mirror)
    return _round(place.x + rx), _round(place.y + ry)


def pin_outward_dir(place: Placement, pin: PinDef) -> tuple[float, float]:
    """Unit vector (sheet space) pointing from the pin position away from
    the symbol body — the direction a stub wire should leave the pin.

    A pin drawn (at px py ang) extends from its position *toward* the body
    along `ang` (in symbol space), so outward is the opposite direction.
    """
    a = math.radians(pin.orientation % 360)
    dx, dy = -round(math.cos(a)), -round(math.sin(a))
    return _sym_to_sheet_vec(dx, dy, place.rotation, place.mirror)


def pin_stub_end(
    place: Placement, pin: PinDef, stub: float = 2.54
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Start (= exact pin position) and end of a stub wire leaving the pin."""
    start = pin_absolute_position(place, pin)
    dx, dy = pin_outward_dir(place, pin)
    end = (_round(start[0] + dx * stub), _round(start[1] + dy * stub))
    return start, end
