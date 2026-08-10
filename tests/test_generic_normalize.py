"""Normalization behaviour that must hold without a device-specific rule."""

from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_relay_flyback, mark_documented_no_connects
from circuitgen.pins import PinType


def test_library_declared_nc_pin_is_marked_without_device_rule():
    lib = "Sensor:Anything"
    symbols = {lib: SymbolDef(lib, "", [
        PinDef("1", "DATA", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("2", "NC", PinType.NOCONNECT, 0, 0, 0, 2.54),
    ])}
    ir = CircuitIR("generic_nc")
    ir.add(Component("U1", lib, "ANY"))
    ir.connect("DATA", ("U1", "1"))
    assert mark_documented_no_connects(ir, symbols) == ["marked documented NC U1.2"]
    assert ir.nc_pins == [("U1", "2")]


def test_flyback_diode_is_rewired_across_named_relay_coil_pins():
    relay, diode = "Relay:Generic", "Device:D"
    symbols = {
        relay: SymbolDef(relay, "", [PinDef("A1", "", PinType.PASSIVE, 0, 0, 0, 2.54), PinDef("A2", "", PinType.PASSIVE, 0, 0, 0, 2.54)], reference_prefix="K"),
        diode: SymbolDef(diode, "", [PinDef("1", "K", PinType.PASSIVE, 0, 0, 0, 2.54), PinDef("2", "A", PinType.PASSIVE, 0, 0, 0, 2.54)], reference_prefix="D"),
    }
    ir = CircuitIR("flyback"); ir.add(Component("K1", relay, "RELAY")); ir.add(Component("D1", diode, "1N4148"))
    ir.connect("COIL_HI", ("K1", "A1")); ir.connect("COIL_LO", ("K1", "A2")); ir.connect("WRONG", ("D1", "1"), ("D1", "2"))
    assert ensure_relay_flyback(ir, symbols) == ["wired D1 across K1 coil A1/A2 for flyback"]
    assert {n.name for n in ir.nets if any(r == "D1" for r, _ in n.nodes)} == {"COIL_HI", "COIL_LO"}


def test_a_fabricated_lib_id_never_reaches_the_emitter():
    """Measured on a real 4-motor board: U11 carried lib_id
    "communication:STS3215 UART" (no such library, and a space in the name)
    and D1 carried "Device:Diode_Schottky" (the symbol is D_Schottky). The
    pipeline dropped both into draft mode and emitted the rest, so the
    schematic on disk was a subset of the IR that compliance and conduction
    were scoring. The netlist round-trip caught it — that gate was the only
    thing standing between this and a silently wrong board.
    """
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("ghosts")
    ir.add(Component("D1", "Device:Diode_Schottky", "Schottky"))
    ir.add(Component("U11", "communication:STS3215 UART", "STS3215 UART"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("+3V3", ("D1", "1"), ("R1", "1"))
    ir.connect("GND", ("D1", "2"), ("R1", "2"))
    ir.connect("TX", ("U11", "TXD"))

    notes = resolve_unknown_symbols(ir, parts)

    # a real symbol under a different name is found through the catalog, and
    # only because it carries the pins this circuit already uses
    assert ir.components["D1"].lib_id == "Device:D_Schottky", notes
    # nothing in the catalog has a TXD pin under that name, so it becomes the
    # box this project already uses for uncatalogued parts — connections kept
    assert ir.components["U11"].lib_id == "Conceptual:STS3215_UART", notes
    assert ("U11", "TXD") in [(r, p) for n in ir.nets if n.name == "TX" for r, p in n.nodes]
    assert ir.components["R1"].lib_id == "Device:R"  # untouched

    # idempotent: every lib_id now resolves or is Conceptual
    assert resolve_unknown_symbols(ir, parts) == []


def test_a_substitute_that_cannot_carry_the_used_pins_is_refused():
    """The catalog lookup must not rescue a part into something unrelated:
    the pin test is the whole safeguard."""
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("wrongfit")
    ir.add(Component("U1", "Device:Diode_Schottky", "Schottky"))
    # a two-pin diode cannot carry pin 7, so no diode may be substituted
    ir.connect("SIG", ("U1", "7"))
    notes = resolve_unknown_symbols(ir, parts)
    assert ir.components["U1"].lib_id.startswith("Conceptual:"), notes
