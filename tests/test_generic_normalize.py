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


def test_a_two_pin_unknown_is_not_rescued_into_a_large_ic():
    """search_parts('SW') ranks Si4734 first on token overlap; that radio
    has pins 1 and 2, so the old first-hit pin test accepted it. A two-pin
    unknown that only uses 1 and 2 'fits' any IC. Rank fewest pins first,
    then prefix.
    """
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("sw")
    ir.add(Component("SW1", "Device:SW", "SW"))
    ir.connect("A", ("SW1", "1"))
    ir.connect("B", ("SW1", "2"))
    notes = resolve_unknown_symbols(ir, parts)
    chosen = ir.components["SW1"].lib_id
    assert chosen != "RF_AM_FM:Si4734-D60-GU", notes
    assert chosen.startswith("Switch:"), notes
    assert len(parts.get_part_pins(chosen)) == 2


def test_unknown_symbol_prefers_a_stem_matched_requested_part():
    """The user already selected Switch:SW_Push. Device:SW is that name with
    the library left off, not a licence to pick any 2-pin switch."""
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("sw")
    ir.add(Component("SW1", "Device:SW", "SW"))
    ir.connect("A", ("SW1", "1"))
    ir.connect("B", ("SW1", "2"))
    notes = resolve_unknown_symbols(ir, parts, preferred=["Device:LED", "Switch:SW_Push"])
    assert ir.components["SW1"].lib_id == "Switch:SW_Push", notes
    # Device:LED is also preferred and also 2-pin prefix-D, but SW is not a
    # stem of LED, so it must not steal a switch ref
    ir2 = CircuitIR("diode")
    ir2.add(Component("D1", "Device:Diode_Schottky", "Schottky"))
    ir2.connect("+3V3", ("D1", "1"))
    ir2.connect("GND", ("D1", "2"))
    resolve_unknown_symbols(ir2, parts, preferred=["Device:LED", "Switch:SW_Push"])
    assert ir2.components["D1"].lib_id == "Device:D_Schottky"


def test_unknown_capacitor_becomes_device_c_not_an_eight_pin_ic():
    """Measured: Capacitor:Cap_0603 ranked Power_Management:CAP006DG because
    that IC has pins 1 and 2 and Device:C is not an FTS hit for Cap_0603.
    Transcription already binds IEEE 315 C to Device:C; design mode did not.
    """
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("cap")
    ir.add(Component("C1", "Capacitor:Cap_0603", "100nF"))
    ir.connect("+3V3", ("C1", "1"))
    ir.connect("GND", ("C1", "2"))
    notes = resolve_unknown_symbols(ir, parts)
    assert ir.components["C1"].lib_id == "Device:C", notes
    assert resolve_unknown_symbols(ir, parts) == []


def test_unknown_capacitor_is_device_c_even_when_the_ref_is_not_c():
    """The same 2-pin Capacitor:Cap_0603. Gating the IEEE generic on the
    reference prefix made C1 → Device:C and U1 → CAP006DG."""
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("cap")
    ir.add(Component("U1", "Capacitor:Cap_0603", "100nF"))
    ir.connect("+3V3", ("U1", "1"))
    ir.connect("GND", ("U1", "2"))
    notes = resolve_unknown_symbols(ir, parts)
    assert ir.components["U1"].lib_id == "Device:C", notes
    assert "CAP006DG" not in ir.components["U1"].lib_id


def test_ieee_passive_generic_is_refused_when_the_used_pins_do_not_fit():
    from circuitgen.normalize import resolve_unknown_symbols
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    ir = CircuitIR("notcap")
    ir.add(Component("C1", "Capacitor:Cap_0603", "100nF"))
    ir.connect("SIG", ("C1", "8"))
    resolve_unknown_symbols(ir, parts)
    assert ir.components["C1"].lib_id != "Device:C"


def test_a_placeholder_beside_the_real_part_is_removed():
    """Measured on a real 4-motor board: a second STM32G474 appeared as
    Conceptual:STM32G474 next to the real MCU_ST_STM32G4:STM32G474CBTx.
    _limit_main_device_copies compares lib_ids and those spellings differ, so
    nothing caught it — the phantom reached the schematic, the sheet split and
    the design explanation."""
    from circuitgen.normalize import merge_duplicate_placeholders
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    real_id = "MCU_ST_STM32G4:STM32G474CBTx"
    ir = CircuitIR("two-mcus")
    ir.add(Component("U1", real_id, "STM32G474"))
    ir.add(Component("U11", "Conceptual:STM32G474", "STM32G474"))
    ir.connect("UART_TX", ("U11", "UART_TX"))
    ir.connect("+3V3", ("U11", "VDD"), ("U1", "1"))

    symbols = parts.load_symbols([real_id])
    from circuitgen.conceptual import resolve_conceptual
    resolve_conceptual(ir, symbols)

    notes = merge_duplicate_placeholders(ir, symbols)
    assert "U11" not in ir.components, notes
    # VDD exists on the real symbol, so that connection moves
    assert ("U1", "1") in [n for net in ir.nets if net.name == "+3V3" for n in net.nodes]
    # UART_TX does not, and that is said out loud rather than dropped quietly
    assert any("UART_TX" in n and "no pin of that name" in n for n in notes), notes
    assert merge_duplicate_placeholders(ir, symbols) == []


def test_two_boxes_for_one_device_collapse_onto_the_wired_one():
    """Three mechanisms drew the same STS3215 servo bus on one board: the
    role-restore block, the uncatalogued-role injection, and the model's own
    component. The copy carrying the connections is the one that survives."""
    from circuitgen.conceptual import resolve_conceptual
    from circuitgen.normalize import merge_duplicate_placeholders

    ir = CircuitIR("three-uarts")
    ir.add(Component("STS3215_UART1", "Conceptual:STS3215_UART", "STS3215"))
    ir.add(Component("U12", "Conceptual:STS3215_UART", "STS3215"))
    ir.connect("UART_TX", ("U12", "TXD"))
    ir.connect("UART_RX", ("U12", "RXD"))

    symbols: dict = {}
    resolve_conceptual(ir, symbols)
    notes = merge_duplicate_placeholders(ir, symbols)
    assert set(ir.components) == {"U12"}, notes
    assert any("carries the most connections" in n for n in notes), notes


def test_a_lone_placeholder_is_left_alone():
    """A box with no real part behind it is the whole point of the mechanism."""
    from circuitgen.conceptual import resolve_conceptual
    from circuitgen.normalize import merge_duplicate_placeholders

    ir = CircuitIR("one-box")
    ir.add(Component("U1", "Conceptual:MY_CUSTOM_RADIO", "RADIO"))
    ir.connect("UART_TX", ("U1", "TX"))
    symbols: dict = {}
    resolve_conceptual(ir, symbols)
    assert merge_duplicate_placeholders(ir, symbols) == []
    assert "U1" in ir.components


def test_a_driver_pin_on_a_rail_is_a_short_and_is_removed():
    """Measured on a 4-motor board: the MOTOR block declared no interface net,
    so the model had nothing to connect its driver to and put EVERY pin on GND
    — including OUTA/OUTB/OUTC, the three phase outputs. Four drivers, each
    shorted to ground on every terminal, and nothing complained: the pins were
    on a net, so the conduction check called them connected.

    agent._filter_ops has refused exactly this for repair ops since the encoder
    incident (A/B/INDEX outputs to GND, ERC 21 -> 58). It had never been
    applied to what synthesis produces, which is where it happens.
    """
    from circuitgen.normalize import free_driver_pins_from_rails
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    lib = "Driver_Motor:DRV8311H"
    symbols = parts.load_symbols([lib, "power:GND"])
    ir = CircuitIR("shorted")
    ir.add(Component("U2", lib, "DRV8311H"))
    ir.add(Component("#PWR01", "power:GND", "GND"))
    sym = symbols[lib]
    ir.connect("GND", ("#PWR01", "1"), *[(("U2"), p.number) for p in sym.pins])

    notes = free_driver_pins_from_rails(ir, symbols)
    left = {p for net in ir.nets for r, p in net.nodes if r == "U2"}
    driver_pins = {
        p.number for p in sym.pins
        if p.etype.name in ("OUTPUT", "TRISTATE", "OPENCOLL", "OPENEMIT")
    }
    assert driver_pins, "the fixture needs driver pins to be meaningful"
    assert not (driver_pins & left), sorted(driver_pins & left)
    assert any("OUTA" in n for n in notes), notes
    # inputs and supply pins are left where they are — tying those is legal
    assert "15" in left  # INHA, an INPUT
    assert free_driver_pins_from_rails(ir, symbols) == []
