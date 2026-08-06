"""pin_absolute_position against real KiCad demo data.

Every case below is a measured triple from
kicad-source-mirror-10.0.5/demos/pic_programmer/pic_programmer.kicad_sch:
a placed symbol's (at ...), a pin's local (at ...) from the file's own
lib_symbols cache, and the wire endpoint that touches that pin. If the
transform disagrees with any of them, stubs/labels will silently miss
pins and KiCad ERC will report unconnected pins.
"""

import pytest

from circuitgen.geometry import Placement, pin_absolute_position, pin_stub_end
from circuitgen.ir import PinDef
from circuitgen.pins import PinType


def pin(number, x, y, orientation):
    return PinDef(
        number=number, name="", etype=PinType.PASSIVE,
        x=x, y=y, orientation=orientation, length=1.27,
    )


DEMO_CASES = [
    # (case, placement, pin local def, expected absolute position)
    ("R2 rot0 pin1", Placement(71.12, 50.8, 0), pin("1", 0, 3.81, 270), (71.12, 46.99)),
    ("R8 rot90 pin1", Placement(119.38, 43.18, 90), pin("1", 0, 3.81, 270), (123.19, 43.18)),
    ("R8 rot90 pin2", Placement(119.38, 43.18, 90), pin("2", 0, -3.81, 90), (115.57, 43.18)),
    ("D1 rot180 pin1", Placement(41.91, 168.91, 180), pin("1", -3.81, 0, 0), (45.72, 168.91)),
    ("D1 rot180 pin2", Placement(41.91, 168.91, 180), pin("2", 3.81, 0, 180), (38.1, 168.91)),
    ("D2 rot270 pin1", Placement(87.63, 36.83, 270), pin("1", -3.81, 0, 0), (87.63, 40.64)),
    ("D2 rot270 pin2", Placement(87.63, 36.83, 270), pin("2", 3.81, 0, 180), (87.63, 33.02)),
    ("U2 mirror-y pin12", Placement(110.49, 106.68, 0, "y"), pin("12", -7.62, 0, 0), (118.11, 106.68)),
    ("U2 mirror-y pin11", Placement(110.49, 106.68, 0, "y"), pin("11", 7.62, 0, 180), (102.87, 106.68)),
    ("Q2 mirror-x pin1", Placement(163.83, 35.56, 0, "x"), pin("1", 2.54, 5.08, 270), (166.37, 40.64)),
    ("Q2 mirror-x pin2", Placement(163.83, 35.56, 0, "x"), pin("2", -5.08, 0, 0), (158.75, 35.56)),
    ("Q2 mirror-x pin3", Placement(163.83, 35.56, 0, "x"), pin("3", 2.54, -5.08, 90), (166.37, 30.48)),
]


@pytest.mark.parametrize("case,place,p,expected", DEMO_CASES, ids=[c[0] for c in DEMO_CASES])
def test_pin_absolute_position_matches_demo(case, place, p, expected):
    assert pin_absolute_position(place, p) == expected


def test_stub_starts_exactly_on_pin():
    place = Placement(63.5, 63.5, 0)
    p = pin("1", -5.08, 0, 0)  # SW_Push pin 1, points right toward body
    start, end = pin_stub_end(place, p, 2.54)
    assert start == pin_absolute_position(place, p)
    # Outward is away from the body: to the left, same Y.
    assert end == (start[0] - 2.54, start[1])


def test_stub_direction_rotates_with_symbol():
    p = pin("1", 0, 3.81, 270)  # R pin 1: top pin, points down toward body
    # rot 0: pin at (X, Y-3.81), outward is up (-Y on the sheet).
    start, end = pin_stub_end(Placement(50.8, 50.8, 0), p)
    assert (start, end) == ((50.8, 46.99), (50.8, 44.45))
    # rot 90: pin at (X+3.81, Y), outward is +X.
    start, end = pin_stub_end(Placement(50.8, 50.8, 90), p)
    assert (start, end) == ((54.61, 50.8), (57.15, 50.8))


def test_grid_alignment_of_stub_ends():
    # All demo-style placements are on the 1.27 grid; stub ends must stay on it.
    p = pin("2", 5.08, 0, 180)
    for rot in (0, 90, 180, 270):
        start, end = pin_stub_end(Placement(63.5, 63.5, rot), p)
        for v in (*start, *end):
            assert abs(v / 1.27 - round(v / 1.27)) < 1e-9
