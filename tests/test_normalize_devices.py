import pytest

from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import (
    complete_known_device_pins,
    ensure_dc_power_entry,
    enforce_requested_part_variants,
    ensure_stm32g4_power_network,
    normalize_common_symbol_aliases,
    sanitize_known_device_nets,
)
from circuitgen.pins import PinType


def _sym(lib_id, pins):
    return SymbolDef(
        lib_id, "", [PinDef(str(no), name, typ, 0, 0, 0, 2.54) for no, name, typ in pins]
    )


def test_vendor_tvs_and_fuse_aliases_use_loadable_kicad_primitives():
    ir = CircuitIR("aliases")
    ir.add(Component("D1", "SparkFun_Custom:D_TVS_24V", "TVS"))
    ir.add(Component("F1", "SparkFun_Custom:Fuse", "Fuse"))
    notes = normalize_common_symbol_aliases(ir)
    assert ir.components["D1"].lib_id == "Device:D_TVS"
    assert ir.components["F1"].lib_id == "Device:Fuse"
    assert len(notes) == 2


def test_sanitize_removes_power_catalog_leaks_and_tja_spi_aliases():
    tja = "Interface_CAN_LIN:TJA1051T"
    pwr = "Converter_ACDC:RAC20-12SK"
    symbols = {
        tja: _sym(tja, [
            (1, "TXD", PinType.INPUT), (2, "GND", PinType.PWRIN),
            (3, "VCC", PinType.PWRIN), (4, "RXD", PinType.OUTPUT),
            (5, "NC", PinType.NOCONNECT), (6, "CANL", PinType.BIDIR),
            (7, "CANH", PinType.BIDIR), (8, "S", PinType.INPUT),
        ]),
        pwr: _sym(pwr, [(1, "AC(N)", PinType.PWRIN), (5, "+Vout", PinType.PWROUT)]),
    }
    ir = CircuitIR("sanitize")
    ir.add(Component("U1", pwr, "ACDC", group="POWER"))
    ir.add(Component("U2", tja, "CAN", group="MCU"))
    ir.connect("POWER_CAN_RX", ("U1", "1"))
    ir.connect("POWER_VCC", ("U1", "5"))
    ir.connect("SPI_MOSI", ("U2", "4"))
    ir.connect("CAN_RX", ("U2", "4"))
    ir.connect("CANH", ("U2", "7"))
    notes = sanitize_known_device_nets(ir, symbols)
    memberships = {
        node: [n.name for n in ir.nets if node in n.nodes]
        for node in [("U1", "1"), ("U1", "5"), ("U2", "4"), ("U2", "7")]
    }
    assert memberships[("U1", "1")] == []
    assert memberships[("U1", "5")] == ["POWER_VCC"]
    assert memberships[("U2", "4")] == ["CAN_RX"]
    assert memberships[("U2", "7")] == ["CANH"]
    assert notes


def test_ac_module_is_replaced_by_fused_dc_battery_entry():
    ir = CircuitIR("power")
    ir.add(Component("U1", "Converter_ACDC:RAC20-12SK", "AC", group="POWER"))
    ir.add(Component("D1", "Device:D_TVS", "24V", group="POWER_REQUIREMENTS"))
    ir.add(Component("F1", "Device:Fuse", "5A", group="POWER_REQUIREMENTS"))
    ir.add(Component("C1", "Device:C", "220uF", group="POWER_REQUIREMENTS"))
    ir.connect("BAD", ("U1", "1"), ("F1", "1"), ("D1", "1"), ("C1", "1"))
    ir.connect("GND", ("U1", "4"), ("F1", "2"), ("D1", "2"), ("C1", "2"))
    notes = ensure_dc_power_entry(ir, "+12V")
    assert "U1" not in ir.components
    battery = next(r for r, c in ir.components.items() if c.value == "BATTERY_IN")
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert {(battery, "1"), ("F1", "1")} <= by_net["BATTERY_RAW"]
    assert {("F1", "2"), ("D1", "1"), ("C1", "1")} <= by_net["+12V"]
    assert {(battery, "2"), ("D1", "2"), ("C1", "2")} <= by_net["GND"]
    assert notes


def test_a_named_variant_replaces_its_sibling_and_moves_nets_by_pin_name():
    """The rule is general: an ordering code the user named is a requirement,
    and swapping packages must move connections by PIN NAME because the
    numbers differ between them. It used to fire on a literal regex for one
    board's MCU and write that board's part number into the value field."""
    from circuitgen.partindex import PartIndex

    old_id = "MCU_ST_STM32G4:STM32G474CBTx"
    old = _sym(old_id, [
        (8, "PA0", PinType.BIDIR), (13, "PA5", PinType.BIDIR),
        (24, "VDD", PinType.PWRIN), (23, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("variant")
    ir.add(Component("U1", old_id, "STM32G474CBTx"))
    ir.connect("PWM", ("U1", "13"))
    ir.connect("+3V3", ("U1", "24"))
    notes = enforce_requested_part_variants(
        ir, "STM32G474RET6 board", {old_id: old}, PartIndex()
    )
    assert ir.components["U1"].lib_id == "MCU_ST_STM32G4:STM32G474RETx", notes
    assert ir.components["U1"].value == "STM32G474RET6"
    # LQFP64 PA5=19 and first VDD=16 in the official KiCad symbol.
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert ("U1", "19") in by_net["PWM"]
    assert ("U1", "16") in by_net["+3V3"]
    assert notes
    # idempotent: the board now holds the requested part, so nothing repeats
    assert enforce_requested_part_variants(
        ir, "STM32G474RET6 board", {old_id: old}, PartIndex()
    ) == []


def test_the_same_rule_applies_to_a_part_it_was_never_written_for():
    """The point of the rewrite: no branch names a device. A request for a
    temperature sensor variant migrates the same way the MCU did."""
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    old_id = "Sensor_Temperature:TMP101"
    try:
        old = parts.load_symbols([old_id])[old_id]
    except Exception:
        import pytest
        pytest.skip("bundled library lacks TMP101")
    ir = CircuitIR("sensor")
    ir.add(Component("U1", old_id, "TMP101"))
    ir.connect("SDA", ("U1", old.pins[0].number))
    notes = enforce_requested_part_variants(
        ir, "TMP100 센서를 씁니다", {old_id: old}, parts
    )
    assert ir.components["U1"].lib_id.endswith("TMP100"), notes


def test_an_unrelated_part_is_never_migrated_onto_the_request():
    """Same-library and a family-length shared prefix are what keep this from
    rewiring a device that merely happens to be in the circuit."""
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    other_id = "Device:R"
    other = _sym(other_id, [(1, "", PinType.PASSIVE), (2, "", PinType.PASSIVE)])
    ir = CircuitIR("unrelated")
    ir.add(Component("R1", other_id, "10k"))
    ir.connect("SIG", ("R1", "1"))
    notes = enforce_requested_part_variants(
        ir, "STM32G474RET6 board", {other_id: other}, parts
    )
    assert ir.components["R1"].lib_id == other_id, notes
    assert notes == []


def test_known_as5048_power_test_and_pwm_completion():
    lid = "Sensor_Magnetic:AS5048A"
    symbols = {lid: _sym(lid, [
        (3, "MISO", PinType.OUTPUT), (5, "TEST", PinType.PASSIVE),
        (11, "VDD5V", PinType.PWRIN), (12, "VDD3V", PinType.PWRIN),
        (13, "GND", PinType.PWRIN), (14, "PWM", PinType.OUTPUT),
    ])}
    ir = CircuitIR("x")
    ir.add(Component("U1", lid, "AS5048A", group="ENC1"))
    complete_known_device_pins(ir, symbols, ["+3V3", "GND"])
    assert {tuple(node) for n in ir.nets for node in n.nodes} >= {
        ("U1", "11"), ("U1", "12"), ("U1", "13")
    }
    assert ("U1", "5") in ir.nc_pins
    assert ("U1", "14") in ir.nc_pins


def test_stm32g4_power_network_adds_per_vdd_and_analog_decoupling():
    lid = "MCU_ST_STM32G4:STM32G474RETx"
    symbols = {lid: _sym(lid, [
        (16, "VDD", PinType.PWRIN), (32, "VDD", PinType.PWRIN),
        (15, "VSS", PinType.PWRIN), (27, "VSSA", PinType.PWRIN),
        (28, "VREF+", PinType.INPUT), (29, "VDDA", PinType.PWRIN),
    ])}
    ir = CircuitIR("x")
    ir.add(Component("U1", lid, "STM32G474", group="MCU"))
    ir.connect("+3V3", ("U1", "16"), ("U1", "32"), ("U1", "28"), ("U1", "29"))
    ir.connect("GND", ("U1", "15"), ("U1", "27"))
    ensure_stm32g4_power_network(ir, symbols)
    caps = [c.value for c in ir.components.values() if c.lib_id == "Device:C"]
    # Figure 16 with two VDD pins: 2 x 100nF (VDD) + 1 x 100nF (VREF+),
    # 1 x 4.7uF bulk, 10nF + 1uF (VDDA), 1uF (VREF+). The previous expectation
    # here (10uF bulk, 100nF on VDDA) came from an AN5093 citation that is not
    # in this repository and disagrees with the datasheet that is.
    assert caps.count("100nF") == 3
    assert caps.count("4.7uF") == 1 and caps.count("10nF") == 1
    assert caps.count("1uF") == 2
    assert not any(c.lib_id == "Device:FerriteBead" for c in ir.components.values())
    on_rail = {n.name for n in ir.nets for r, p in n.nodes
               if r == "U1" and p in {"28", "29"}}
    assert on_rail == {"+3V3"}, "3.11.1: VDDA belongs on the VDD rail"


def test_stacked_pins_join_their_wired_sibling_net():
    from circuitgen.normalize import unify_stacked_pins

    sym = SymbolDef("Filter:Stacky", "", [
        PinDef("1", "IN", PinType.INPUT, -10.16, 0, 0, 2.54),
        PinDef("4", "V-", PinType.PWRIN, 0, -17.78, 90, 2.54),
        PinDef("7", "V-", PinType.PWRIN, 0, -17.78, 90, 2.54),
        PinDef("14", "V-", PinType.PWRIN, 0, -17.78, 90, 2.54),
    ])
    ir = CircuitIR("stack")
    ir.add(Component("U1", "Filter:Stacky", "F"))
    ir.connect("GND", ("U1", "4"))
    ir.nc_pins = [("U1", "14")]
    notes = unify_stacked_pins(ir, {"Filter:Stacky": sym})
    gnd = next(n for n in ir.nets if n.name == "GND")
    assert {p for _r, p in gnd.nodes} == {"4", "7", "14"}
    assert ("U1", "14") not in ir.nc_pins
    assert len(notes) == 2
    # members on different nets = real short: left alone for self-ERC
    ir2 = CircuitIR("short")
    ir2.add(Component("U1", "Filter:Stacky", "F"))
    ir2.connect("GND", ("U1", "4"))
    ir2.connect("+3V3", ("U1", "7"))
    assert unify_stacked_pins(ir2, {"Filter:Stacky": sym}) == []


def test_documented_nc_pins_are_forcibly_disconnected():
    from circuitgen.normalize import mark_documented_no_connects

    sym = _sym("Sensor:X", [
        (1, "SDA", PinType.BIDIR),
        (3, "NC", PinType.NOCONNECT),
    ])
    ir = CircuitIR("nc")
    ir.add(Component("U1", "Sensor:X", "X"))
    ir.connect("SDA", ("U1", "1"))
    ir.connect("STRAY", ("U1", "3"))  # model wired a documented-NC pin
    notes = mark_documented_no_connects(ir, {"Sensor:X": sym})
    assert any("disconnected documented NC U1.3" in n for n in notes)
    assert not any((r, p) == ("U1", "3") for n in ir.nets for r, p in n.nodes)
    assert ("U1", "3") in ir.nc_pins
    assert any(n.name == "SDA" for n in ir.nets)  # untouched


def test_duplicate_pin_membership_moves_to_free_pin_of_two_pin_passive():
    from circuitgen.normalize import sanitize_known_device_nets

    r = _sym("Device:R", [(1, "~", PinType.PASSIVE), (2, "~", PinType.PASSIVE)])
    led = _sym("Device:LED", [(1, "K", PinType.PASSIVE), (2, "A", PinType.PASSIVE)])
    ir = CircuitIR("dup")
    ir.add(Component("R1", "Device:R", "330R"))
    ir.add(Component("D1", "Device:LED", "LED"))
    # model piled both nets on R1.2; R1.1 dangles
    ir.connect("SW_R", ("R1", "2"))
    ir.connect("R_LED", ("R1", "2"), ("D1", "2"))
    notes = sanitize_known_device_nets(ir, {"Device:R": r, "Device:LED": led})
    assert any("moved R1.2 duplicate membership" in n for n in notes), notes
    nets = {n.name: sorted(n.nodes) for n in ir.nets}
    # one membership stays on pin 2, the other lands on the free pin 1
    all_r1 = sorted(node for n in ir.nets for node in n.nodes if node[0] == "R1")
    assert all_r1 == [("R1", "1"), ("R1", "2")]
    assert ("D1", "2") in nets["R_LED"]


def test_power_block_keeps_logic_level_enable_and_reset():
    """An MCU sequencing a regulator's EN pin is standard design; only a
    digital net landing on a SUPPLY pin is a catalog leak."""
    from circuitgen.normalize import sanitize_known_device_nets

    reg = _sym("Regulator_Linear:X", [
        (1, "VIN", PinType.PWRIN), (2, "GND", PinType.PWRIN),
        (3, "VOUT", PinType.PWROUT), (4, "EN", PinType.INPUT),
    ])
    mcu = _sym("MCU:X", [(1, "PA0", PinType.BIDIR)])
    symbols = {"Regulator_Linear:X": reg, "MCU:X": mcu}

    ir = CircuitIR("en")
    ir.add(Component("U1", "Regulator_Linear:X", "REG", "", "POWER"))
    ir.add(Component("U2", "MCU:X", "MCU", "", "MCU"))
    ir.connect("EN", ("U1", "4"), ("U2", "1"))
    sanitize_known_device_nets(ir, symbols)
    assert [(n.name, sorted(n.nodes)) for n in ir.nets] == [
        ("EN", [("U1", "4"), ("U2", "1")])
    ]

    leak = CircuitIR("leak")
    leak.add(Component("U1", "Regulator_Linear:X", "REG", "", "POWER"))
    leak.add(Component("U2", "MCU:X", "MCU", "", "MCU"))
    leak.connect("SPI_MOSI", ("U1", "1"), ("U2", "1"))  # SPI on VIN: impossible
    sanitize_known_device_nets(leak, symbols)
    assert [n for n in leak.nets if ("U1", "1") in n.nodes] == []


@pytest.mark.parametrize("high,low", [
    ("CANH", "CANL"), ("CAN_H", "CAN_L"), ("CAN_HIGH", "CAN_LOW"),
])
def test_tja1051_whitelist_accepts_standard_bus_net_spellings(high, low):
    """The whitelist guards against a bus pin wired to an unrelated signal;
    it must not delete correct wiring over a naming convention."""
    from circuitgen.normalize import sanitize_known_device_nets
    from circuitgen.symbols import load_symbols

    lib = "Interface_CAN_LIN:TJA1051T"
    symbols = load_symbols([lib])
    ir = CircuitIR("can")
    ir.add(Component("U1", lib, "TJA1051T"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x02", "BUS"))
    ir.connect(high, ("U1", "7"), ("J1", "1"))
    ir.connect(low, ("U1", "6"), ("J1", "2"))
    sanitize_known_device_nets(ir, symbols)
    assert sorted(n.name for n in ir.nets) == sorted([high, low])


@pytest.mark.parametrize("rails,expected", [
    (["+12V", "GND", "+5V"], "+5V"),      # not the first-listed rail
    (["+12V", "GND", "+3V3"], "+3V3"),
    (["+3V3", "GND"], "+3V3"),
    (["+24V", "GND"], None),              # no plausible logic rail: refuse
])
def test_logic_rail_is_the_lowest_supply_not_the_first_listed(rails, expected):
    """Picking by list order tied a 3.3V MCU's VDD to +12V — ERC-clean and
    part-destroying."""
    from circuitgen.netnames import logic_rail

    assert logic_rail(rails) == expected


def test_unsafe_logic_rail_fails_loudly_not_silently():
    """Refusing to wire is only correct if the refusal is VISIBLE. The pattern
    path blanket-NCs unbound hub pins and erc.py skips NC pins, so leaving the
    marker in place produced an unpowered MCU scoring ERC 0."""
    from circuitgen.erc import check_circuit
    from circuitgen.normalize import complete_known_device_pins
    from circuitgen.symbols import load_symbols

    lib = "MCU_ST_STM32G4:STM32G474RETx"
    symbols = load_symbols([lib])
    sym = symbols[lib]
    supply = [p.number for p in sym.pins if p.name.upper() in ("VDD", "VDDA", "VBAT", "VREF+")]

    ir = CircuitIR("hv")
    ir.add(Component("U1", lib, "STM32G474RETx"))
    ir.nc_pins = [("U1", p.number) for p in sym.pins if not p.hidden]  # pattern-path closure
    complete_known_device_pins(ir, symbols, ["+24V", "GND"])

    # nothing tied to the 24V rail ...
    assert not [n for n in ir.nets if any(r == "U1" and p in supply for r, p in n.nodes)]
    # ... and the pins are exposed, so self-ERC can see them
    findings = [str(p) for p in check_circuit(ir, symbols)]
    assert [f for f in findings if "unconnected" in f.lower()]


def test_motor_rail_does_not_inherit_the_logic_rail_refusal():
    """A DRV8311 VM pin spans 4.5-35V, so a +24V-only board is legitimate for
    it even where no logic rail is safe."""
    from circuitgen.normalize import complete_known_device_pins
    from circuitgen.symbols import load_symbols

    lib = "Driver_Motor:DRV8311H"
    symbols = load_symbols([lib])
    ir = CircuitIR("vm")
    ir.add(Component("U1", lib, "DRV8311H"))
    complete_known_device_pins(ir, symbols, ["+24V", "GND"])
    assert [n.name for n in ir.nets if any(r == "U1" and p == "8" for r, p in n.nodes)] == ["+24V"]


# ---- generic power-pin completion (residual of the device table) -------------


def test_vendor_suffixed_ground_pins_are_grounds_not_positive_supplies():
    """VSSA and VSSX are grounds, and GROUND_NAMES matches canonical NET names
    exactly. Without the prefix test they read as positive supplies and could
    be tied to the logic rail — a dead short. Two distinct ground NAMES then
    mean separate domains, so they are refused rather than merged."""
    from circuitgen.netnames import is_ground_pin
    from circuitgen.normalize import complete_generic_power_pins

    assert is_ground_pin("VSSA") and is_ground_pin("VSSX") and is_ground_pin("PGND")
    assert not is_ground_pin("VDD") and not is_ground_pin("VDDX")

    sym = _sym("Vendor:CHIP", [
        (1, "VDD", PinType.PWRIN), (2, "VSSA", PinType.PWRIN),
        (3, "VSSX", PinType.PWRIN), (4, "IO", PinType.BIDIR),
    ])
    ir = CircuitIR("g")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    notes = complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "GND"])
    assert ir.nets == []
    assert any("separate ground domains" in n for n in notes)


def test_one_ground_name_many_pins_is_a_stack_and_gets_wired():
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [
        (1, "VSS", PinType.PWRIN), (2, "VSS", PinType.PWRIN),
        (3, "VSS", PinType.PWRIN), (4, "IO", PinType.BIDIR),
    ])
    ir = CircuitIR("g")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    notes = complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "GND"])
    assert {p for r, p in ir.nets[0].nodes} == {"1", "2", "3"}
    assert ir.nets[0].name == "GND"
    assert any("3 ground pin(s)" in n for n in notes)


def test_a_part_with_two_positive_supply_names_is_left_loud():
    """MC68HC912 has VDD and VDDX; an op-amp has V+ and V-. Tying either to the
    logic rail would be a guess, and a wrong guess is destructive."""
    from circuitgen.normalize import complete_generic_power_pins

    opamp = _sym("Amplifier_Operational:X", [
        (1, "V+", PinType.PWRIN), (2, "V-", PinType.PWRIN), (3, "OUT", PinType.OUTPUT),
    ])
    ir = CircuitIR("g")
    ir.add(Component("U1", "Amplifier_Operational:X", "X"))
    notes = complete_generic_power_pins(ir, {"Amplifier_Operational:X": opamp}, ["+3V3", "GND"])
    assert ir.nets == []
    assert any("ambiguous" in n for n in notes)


def test_generic_completion_never_overrides_an_existing_connection():
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [(1, "VDD", PinType.PWRIN), (2, "VSS", PinType.PWRIN)])
    ir = CircuitIR("g")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.connect("+5V", ("U1", "1"))
    complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "GND"])
    on = {n.name for n in ir.nets if ("U1", "1") in n.nodes}
    assert on == {"+5V"}, "an existing rail choice is not second-guessed"


def test_no_safe_rail_leaves_supply_pins_unconnected_not_nc():
    """A 24V-only board has no plausible logic rail. The pin must stay visibly
    unconnected — marking it NC is what made an unpowered MCU score ERC 0."""
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [(1, "VDD", PinType.PWRIN), (2, "VSS", PinType.PWRIN)])
    ir = CircuitIR("g")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.nc_pins.append(("U1", "1"))
    notes = complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+24V", "GND"])
    assert any("plausible logic supply" in n for n in notes)
    assert ("U1", "1") not in ir.nc_pins, "a stale NC marker would hide the refusal"
    assert not any(n.name not in ("GND",) and ("U1", "1") in n.nodes for n in ir.nets)


def test_unresolvable_footprint_is_cleared_rather_than_blocking_the_build():
    """A 7B wrote 'Connector:LEMO4:LEMO4_4P' — a name with two colons that
    exists nowhere. That is a hard footprint_unknown error, so an otherwise
    ERC-0, connectivity-clean board could never pass over a layout attribute."""
    from circuitgen.fp_checks import assign_footprints, check_footprints
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    if not parts.has_footprints():
        pytest.skip("footprint index not built")
    lib_id = "Connector:LEMO4"
    sym = parts.load_symbols([lib_id])[lib_id]
    ir = CircuitIR("fp")
    ir.add(Component("J1", lib_id, "LEMO4", "Connector:LEMO4:LEMO4_4P"))
    symbols = {lib_id: sym}

    before = check_footprints(ir, symbols, parts)
    assert [i.rule for i in before] == ["footprint_unknown"]

    notes = assign_footprints(ir, symbols, parts)
    assert ir.components["J1"].footprint == ""
    assert any("cleared" in n for n in notes)
    assert check_footprints(ir, symbols, parts) == []


def test_named_crystal_and_pin_header_packages_select_the_requested_family():
    from circuitgen.fp_checks import assign_footprints
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    if not parts.has_footprints():
        pytest.skip("footprint index not built")
    symbols = parts.load_symbols(["Device:Crystal", "Connector_Generic:Conn_02x03_Odd_Even"])
    ir = CircuitIR("named_packages")
    ir.add(Component("Y1", "Device:Crystal", "16MHz"))
    ir.add(Component("J1", "Connector_Generic:Conn_02x03_Odd_Even", "ICSP"))

    assign_footprints(
        ir, symbols, parts,
        requested_packages={"Y1": "HC-49/SD SMD", "J1": "2x3 Pin Header"},
    )

    assert "HC49-SD" in ir.components["Y1"].footprint
    assert "PINHEADER" in ir.components["J1"].footprint.upper()
    assert "P2.54MM" in ir.components["J1"].footprint.upper()


# ---- guards found by adversarial review (each has a reproduced counterexample)


def test_isolated_ground_domains_are_never_merged():
    """ADuM1201 has GND1 and GND2; tying them destroys the isolation barrier
    the part exists for. 3,950 catalog symbols have >1 distinct ground name."""
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Isolator:ADUM1201", [
        (1, "VDD1", PinType.PWRIN), (2, "GND1", PinType.PWRIN),
        (3, "VDD2", PinType.PWRIN), (4, "GND2", PinType.PWRIN),
    ])
    ir = CircuitIR("iso")
    ir.add(Component("U1", "Isolator:ADUM1201", "ADUM1201"))
    notes = complete_generic_power_pins(ir, {"Isolator:ADUM1201": sym}, ["+3V3", "GND"])
    assert ir.nets == [], "no ground net was created"
    assert any("separate ground domains" in n for n in notes)


def test_unknown_part_supply_is_refused_without_a_datasheet_warrant():
    """logic_rail returns the lowest rail <= 5.5 V, which is a coin flip for an
    unknown part: a 5 V CPU lands on +3V3, a 3.3 V flash lands on +5V."""
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("CPU_NXP_68000:MC68332", [
        (1, "VDD", PinType.PWRIN), (2, "VDD", PinType.PWRIN),
        (3, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("cpu")
    ir.add(Component("U1", "CPU_NXP_68000:MC68332", "MC68332"))
    notes = complete_generic_power_pins(ir, {"CPU_NXP_68000:MC68332": sym}, ["+5V", "GND", "+3V3"])
    on = {n.name for n in ir.nets if ("U1", "1") in n.nodes}
    assert on == set(), "VDD must stay loud, not be guessed onto a rail"
    assert any("no datasheet limits are recorded" in n for n in notes)
    # grounds are unambiguous and still get wired
    assert {n.name for n in ir.nets if ("U1", "3") in n.nodes} == {"GND"}


def test_a_recorded_device_within_range_is_wired():
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("MCU_ST_STM32G4:STM32G474RETx", [
        (1, "VDD", PinType.PWRIN), (2, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("ok")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx"))
    notes = complete_generic_power_pins(ir, {"MCU_ST_STM32G4:STM32G474RETx": sym}, ["+3V3", "GND"])
    assert {n.name for n in ir.nets if ("U1", "1") in n.nodes} == {"+3V3"}
    assert any("datasheet range confirms it" in n for n in notes)


def test_a_lone_vplus_with_recorded_limits_is_wired():
    """TMP100's only positive name is V+. An op-amp's V+/V- pair stays refused."""
    from circuitgen.normalize import complete_generic_power_pins

    lib = "Sensor_Temperature:TMP100"
    sym = _sym(lib, [
        (1, "SCL", PinType.BIDIR), (2, "GND", PinType.PWRIN),
        (4, "V+", PinType.PWRIN), (6, "SDA", PinType.BIDIR),
    ])
    ir = CircuitIR("vplus")
    ir.add(Component("U2", lib, "TMP100"))
    notes = complete_generic_power_pins(ir, {lib: sym}, ["+3V3", "GND"])
    assert {n.name for n in ir.nets if ("U2", "4") in n.nodes} == {"+3V3"}
    assert {n.name for n in ir.nets if ("U2", "2") in n.nodes} == {"GND"}
    assert any("datasheet range confirms it" in n for n in notes)


def test_supply_pin_on_a_signal_is_detached_then_wired_when_limits_exist():
    """Shares check_requested_rail_reach: V+ on SCL is not a rail choice."""
    from circuitgen.normalize import (
        complete_generic_power_pins,
        detach_supply_pins_from_nonsupply_nets,
    )

    lib = "Sensor_Temperature:TMP100"
    symbols = {lib: _sym(lib, [
        (1, "SCL", PinType.BIDIR), (2, "GND", PinType.PWRIN),
        (4, "V+", PinType.PWRIN), (6, "SDA", PinType.BIDIR),
    ])}
    ir = CircuitIR("miswired")
    ir.add(Component("U2", lib, "TMP100"))
    ir.connect("SCL", ("U2", "1"), ("U2", "4"))
    ir.connect("GND", ("U2", "2"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    notes = detach_supply_pins_from_nonsupply_nets(ir, symbols, spec)
    assert any("U2.4" in n and "SCL" in n for n in notes)
    assert not any(("U2", "4") in n.nodes for n in ir.nets)
    assert {n.name for n in ir.nets if ("U2", "1") in n.nodes} == {"SCL"}

    notes += complete_generic_power_pins(ir, symbols, ["+3V3", "GND"])
    assert {n.name for n in ir.nets if ("U2", "4") in n.nodes} == {"+3V3"}

    again = (
        detach_supply_pins_from_nonsupply_nets(ir, symbols, spec)
        + complete_generic_power_pins(ir, symbols, ["+3V3", "GND"])
    )
    assert {n.name for n in ir.nets if ("U2", "4") in n.nodes} == {"+3V3"}
    assert {n.name for n in ir.nets if ("U2", "1") in n.nodes} == {"SCL"}
    assert not any("disconnected" in n for n in again)


def test_detach_does_not_second_guess_an_existing_rail():
    from circuitgen.normalize import detach_supply_pins_from_nonsupply_nets

    lib = "Vendor:CHIP"
    symbols = {lib: _sym(lib, [
        (1, "VDD", PinType.PWRIN), (2, "VSS", PinType.PWRIN),
    ])}
    ir = CircuitIR("rail")
    ir.add(Component("U1", lib, "CHIP"))
    ir.connect("+5V", ("U1", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}
    notes = detach_supply_pins_from_nonsupply_nets(ir, symbols, spec)
    assert {n.name for n in ir.nets if ("U1", "1") in n.nodes} == {"+5V"}
    assert notes == []


def test_partially_wired_supplies_never_bridge_two_rails():
    """Some VDD pins already on +5V and the rest sent to +3V3 would short the
    rails through the die's supply bus."""
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [
        (1, "VDD", PinType.PWRIN), (2, "VDD", PinType.PWRIN),
        (3, "VDD", PinType.PWRIN), (4, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("split")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.connect("+5V", ("U1", "1"))
    ir.connect("+3V3", ("U1", "2"))
    notes = complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "+5V", "GND"])
    assert {n.name for n in ir.nets if ("U1", "3") in n.nodes} == set()
    assert any("already span" in n for n in notes)


def test_remaining_supplies_join_the_rail_their_siblings_use():
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [
        (1, "VDD", PinType.PWRIN), (2, "VDD", PinType.PWRIN),
        (3, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("join")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.connect("+5V", ("U1", "1"))
    complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "+5V", "GND"])
    assert {n.name for n in ir.nets if ("U1", "2") in n.nodes} == {"+5V"}


def test_a_refused_supply_pin_loses_a_stale_nc_marker():
    """The pattern path blanket-NCs unbound hub pins; leaving the marker makes
    the refusal silent, because erc.py skips NC pins."""
    from circuitgen.normalize import complete_generic_power_pins

    sym = _sym("Vendor:CHIP", [(1, "VDD", PinType.PWRIN), (2, "VSS", PinType.PWRIN)])
    ir = CircuitIR("nc")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.nc_pins.extend([("U1", "1")])
    complete_generic_power_pins(ir, {"Vendor:CHIP": sym}, ["+3V3", "GND"])
    assert ("U1", "1") not in ir.nc_pins, "refusal must be visible to ERC"


def test_stm32g4_decoupling_matches_the_datasheet_figure():
    """DS12288 Rev 6 §5.1.6 Figure 16 (DS_stm32g474ve.pdf, pdf page 80):

        VDD/VSS    n x 100 nF + 1 x 4.7 uF
        VDDA/VSSA  10 nF + 1 uF
        VREF+      100 nF + 1 uF

    §5.3.19 states decoupling "must be performed as shown in Figure 16", so
    these are not judgement calls. STM32G474RETx (LQFP64) has four VDD pins.
    """
    from collections import Counter

    from circuitgen.normalize import ensure_stm32g4_power_network
    from circuitgen.symbols import load_symbols

    lid = "MCU_ST_STM32G4:STM32G474RETx"
    # the whole symbol set, as the pipeline resolves it before each pass: the
    # capacitors this pass adds must be identifiable on the next call or it
    # cannot see its own work
    symbols = load_symbols([lid, "Device:C"])
    ir = CircuitIR("g4")
    ir.add(Component("U1", lid, "STM32G474RET6", group="MCU"))
    ir.connect("+3V3", ("U1", "16"))

    notes = ensure_stm32g4_power_network(ir, symbols, "+3V3")
    caps = Counter(c.value for c in ir.components.values() if c.lib_id == "Device:C")
    assert dict(caps) == {"100nF": 5, "4.7uF": 1, "10nF": 1, "1uF": 2}

    sym = symbols[lid]
    def nets_of(pin_name):
        pins = {p.number for p in sym.pins if p.name.upper() == pin_name}
        return {n.name for n in ir.nets for r, pn in n.nodes if r == "U1" and pn in pins}

    # 3.11.1: VDDA "should preferably be connected to VDD when these
    # peripherals are not used"
    assert nets_of("VDD") == nets_of("VDDA") == nets_of("VREF+") == {"+3V3"}
    assert nets_of("VSS") == nets_of("VSSA") == {"GND"}

    # 3.13: HSI16 can drive the PLL to 170 MHz, so a crystal is optional and
    # the answer belongs in the record rather than in a silently added part
    assert any("no external crystal" in n for n in notes)
    assert not any("Crystal" in c.lib_id for c in ir.components.values())

    before = len(ir.components)
    ensure_stm32g4_power_network(ir, symbols, "+3V3")
    assert len(ir.components) == before, "the pass must be idempotent"


def test_i2c_pullups_are_added_once_and_only_when_missing():
    """I2C is open-drain: an open-drain output has no high-side device, so a
    valid HIGH exists only through an external pull-up to the supply (Floyd,
    Digital Fundamentals 11ed 15-2/15-3, pdf page 872). 10k is the typical
    value (PEFI 12.6.9, pdf page 1246).

    Presence is judged on topology, not on labels. The pass this replaced
    keyed on the rail name, so renaming the rail left the old set behind and
    added a second one.
    """
    from circuitgen.normalize import ensure_i2c_pullups
    from circuitgen.symbols import load_symbols

    sensor, res = "Sensor_Temperature:TMP100", "Device:R"
    symbols = load_symbols([sensor, res, "power:+3V3", "power:VCC"])

    def board(pullup_rail=None):
        ir = CircuitIR("i2c")
        ir.add(Component("U1", sensor, "TMP100"))
        ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
        ir.connect("+3V3", ("#PWR01", "1"))
        ir.connect("SDA", ("U1", "6"))
        ir.connect("SCL", ("U1", "1"))
        if pullup_rail:
            ir.add(Component("#PWR02", f"power:{pullup_rail}", pullup_rail))
            ir.connect(pullup_rail, ("#PWR02", "1"))
            for i, line in enumerate(("SDA", "SCL"), 1):
                ir.add(Component(f"R{i}", res, "4.7k"))
                ir.connect(line, (f"R{i}", "1"))
                ir.connect(pullup_rail, (f"R{i}", "2"))
        return ir

    bare = board()
    assert len(ensure_i2c_pullups(bare, symbols, "+3V3")) == 2
    values = [c.value for c in bare.components.values() if c.lib_id == res]
    assert values == ["10k", "10k"]
    assert ensure_i2c_pullups(bare, symbols, "+3V3") == [], "must be idempotent"

    # the model's own choice of value is respected, not doubled
    already = board("+3V3")
    assert ensure_i2c_pullups(already, symbols, "+3V3") == []

    # and a pull-up to a DIFFERENT real supply still counts as a pull-up
    other_rail = board("VCC")
    assert ensure_i2c_pullups(other_rail, symbols, "+3V3") == []


def test_the_checker_and_the_fixer_share_one_definition_of_an_i2c_net():
    """If they disagreed, ERC would demand a pull-up the fixer never adds."""
    from circuitgen.erc import is_i2c_net
    from circuitgen.normalize import ensure_i2c_pullups
    from circuitgen.symbols import load_symbols

    sensor = "Sensor_Temperature:TMP100"
    symbols = load_symbols([sensor, "Device:R", "power:+3V3"])
    ir = CircuitIR("i2c")
    ir.add(Component("U1", sensor, "TMP100"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.connect("+3V3", ("#PWR01", "1"))
    ir.connect("BUS_A", ("U1", "6"))          # SDA pin, non-obvious net name
    ir.connect("UNRELATED", ("U1", "3"))      # ADD1, not a bus line

    buses = {n.name for n in ir.nets if is_i2c_net(ir, symbols, n)}
    assert buses == {"BUS_A"}
    ensure_i2c_pullups(ir, symbols, "+3V3")
    pulled = {
        n.name for n in ir.nets
        for r, _p in n.nodes
        if ir.components.get(r) and ir.components[r].lib_id == "Device:R"
    }
    assert "BUS_A" in pulled and "UNRELATED" not in pulled


def test_a_resistor_already_bridging_two_nets_is_never_repurposed():
    """The pass this replaced hijacked an unrelated rail-to-GND bleeder."""
    from circuitgen.normalize import ensure_i2c_pullups
    from circuitgen.symbols import load_symbols

    sensor, res = "Sensor_Temperature:TMP100", "Device:R"
    symbols = load_symbols([sensor, res, "power:+3V3"])
    ir = CircuitIR("i2c")
    ir.add(Component("U1", sensor, "TMP100"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.connect("+3V3", ("#PWR01", "1"))
    ir.connect("SDA", ("U1", "6"))
    ir.add(Component("R9", res, "1M"))            # a bleeder, both legs used
    ir.connect("SDA", ("R9", "1"))
    ir.connect("GND", ("R9", "2"))

    ensure_i2c_pullups(ir, symbols, "+3V3")
    on = {n.name for n in ir.nets if any(r == "R9" for r, _p in n.nodes)}
    assert on == {"SDA", "GND"}, "the bleeder keeps its own job"
    assert any(c.value == "10k" for c in ir.components.values()), "a real pull-up was added"


def test_a_capacitor_across_sda_and_scl_moves_onto_the_supply():
    """017 C1 (0.01 µF) sat on SDA and SCL. Figure 12 is V+ to GND."""
    from circuitgen.erc import capacitors_across_i2c_lines, check_circuit
    from circuitgen.normalize import detach_capacitors_across_i2c_lines
    from circuitgen.symbols import load_symbols

    sensor = "Sensor_Temperature:TMP100"
    symbols = load_symbols([
        sensor, "Device:C", "Device:R", "power:+3V3", "power:GND",
    ])
    ir = CircuitIR("i2c-c")
    ir.add(Component("U1", sensor, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.add(Component("C2", "Device:C", "100nF"))
    ir.add(Component("R9", "Device:R", "10k"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+3V3", ("#PWR01", "1"), ("U1", "4"), ("C2", "1"))
    ir.connect("GND", ("#PWR02", "1"), ("U1", "2"), ("C2", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"), ("R9", "1"))
    ir.connect("+3V3", ("R9", "2"))

    before = capacitors_across_i2c_lines(ir, symbols)
    assert before == [("C1", "SDA", "SCL")] or before == [("C1", "SCL", "SDA")]
    assert any(i.rule == "capacitor_across_i2c" for i in check_circuit(ir, symbols))

    notes = detach_capacitors_across_i2c_lines(ir, symbols, "+3V3")
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    c2 = {n.name for n in ir.nets if any(r == "C2" for r, _ in n.nodes)}
    r9 = {n.name for n in ir.nets if any(r == "R9" for r, _ in n.nodes)}
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert c1 == {"+3V3", "GND"}, notes
    assert c2 == {"+3V3", "GND"}
    assert r9 == {"SCL", "+3V3"}
    assert ("C1", "1") not in sda.nodes and ("C1", "2") not in sda.nodes
    assert ("C1", "1") not in scl.nodes and ("C1", "2") not in scl.nodes
    assert capacitors_across_i2c_lines(ir, symbols) == []
    assert not any(i.rule == "capacitor_across_i2c" for i in check_circuit(ir, symbols))
    assert detach_capacitors_across_i2c_lines(ir, symbols, "+3V3") == []


def _two_pin_c():
    return SymbolDef(
        "Device:C",
        "",
        [
            PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("2", "~", PinType.PASSIVE, 0, 0, 180, 2.54),
        ],
        reference_prefix="C",
    )


def _two_pin_r():
    return SymbolDef(
        "Device:R",
        "",
        [
            PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("2", "~", PinType.PASSIVE, 0, 0, 180, 2.54),
        ],
        reference_prefix="R",
    )


def _tmp100_pins():
    return _sym("Sensor_Temperature:TMP100", [
        (1, "SCL", PinType.BIDIR), (2, "GND", PinType.PWRIN),
        (4, "V+", PinType.PWRIN), (6, "SDA", PinType.BIDIR),
    ])


def test_bypass_uses_the_i2c_device_ground_not_the_first_gnd_net():
    """AGND listed first used to steal the bypass off TMP100 GND."""
    from circuitgen.erc import capacitors_across_i2c_lines
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    amp = "Amplifier:X"
    symbols = {
        tmp: _tmp100_pins(),
        "Device:C": _two_pin_c(),
        amp: _sym(amp, [(1, "AGND", PinType.PWRIN), (2, "OUT", PinType.OUTPUT)]),
    }
    ir = CircuitIR("agnd-first")
    ir.add(Component("U9", amp, "X"))
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.connect("AGND", ("U9", "1"))
    ir.connect("+3V3", ("U1", "4"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    assert [n.name for n in ir.nets if n.name in {"AGND", "GND"}][0] == "AGND"
    assert capacitors_across_i2c_lines(ir, symbols)
    detach_capacitors_across_i2c_lines(ir, symbols, "+3V3")
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}
    assert not any(r == "C1" for n in ir.nets if n.name == "AGND" for r, _ in n.nodes)


def test_bypass_follows_the_sensor_vplus_net_not_the_rail_argument():
    """V+ on VCC used to be NC'd because the caller passed +3V3."""
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _two_pin_c()}
    ir = CircuitIR("vcc-named")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.connect("VCC", ("U1", "4"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    detach_capacitors_across_i2c_lines(ir, symbols, "+3V3")
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"VCC", "GND"}


def _mcu_vbat_before_vdd():
    """STM32G474 PWRIN order starts at VBAT; two VDD pins share +3V3."""
    return _sym("MCU:X", [
        (1, "VBAT", PinType.PWRIN),
        (15, "VSS", PinType.PWRIN),
        (16, "VDD", PinType.PWRIN),
        (32, "VDD", PinType.PWRIN),
        (49, "SCL", PinType.BIDIR),
        (50, "SDA", PinType.BIDIR),
    ])


def test_bypass_uses_the_rail_most_of_the_ic_power_pins_share():
    """Pin-list first PWRIN is VBAT; Figure 12 is the VDD rail those pins share."""
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    mcu = "MCU:X"
    symbols = {mcu: _mcu_vbat_before_vdd(), "Device:C": _two_pin_c()}
    ir = CircuitIR("vbat-first")
    ir.add(Component("U1", mcu, "X"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.connect("VBAT", ("U1", "1"))
    ir.connect("GND", ("U1", "15"))
    ir.connect("+3V3", ("U1", "16"), ("U1", "32"))
    ir.connect("SDA", ("U1", "50"), ("C1", "1"))
    ir.connect("SCL", ("U1", "49"), ("C1", "2"))
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}
    assert not any(r == "C1" for n in ir.nets if n.name == "VBAT" for r, _ in n.nodes)


def test_bypass_does_not_fall_onto_vbat_when_the_sensor_supply_is_open():
    """TMP100 V+ unconnected used to skip the sensor and park C1 on MCU VBAT."""
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    mcu = "MCU:X"
    symbols = {
        tmp: _tmp100_pins(),
        mcu: _mcu_vbat_before_vdd(),
        "Device:C": _two_pin_c(),
    }
    ir = CircuitIR("open-vplus")
    ir.add(Component("U1", mcu, "X"))
    ir.add(Component("U2", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.connect("VBAT", ("U1", "1"))
    ir.connect("GND", ("U1", "15"), ("U2", "2"))
    ir.connect("+3V3", ("U1", "16"), ("U1", "32"))
    ir.connect("SDA", ("U1", "50"), ("U2", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "49"), ("U2", "1"), ("C1", "2"))
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}


def _three_pad_c():
    return SymbolDef(
        "Device:C",
        "",
        [
            PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("2", "~", PinType.PASSIVE, 0, 0, 180, 2.54),
            PinDef("3", "~", PinType.PASSIVE, 0, 0, 90, 2.54),
        ],
        reference_prefix="C",
    )


def _pwr(lib_id, name):
    return SymbolDef(
        lib_id, "",
        [PinDef("1", name, PinType.PWRIN, 0, 0, 0, 2.54)],
        is_power=True, reference_prefix="#PWR",
    )


def test_a_capacitor_with_an_unused_third_pad_still_leaves_the_bus():
    from circuitgen.erc import capacitors_across_i2c_lines, check_circuit
    from circuitgen.normalize import detach_capacitors_across_i2c_lines, ensure_pwr_flags

    tmp = "Sensor_Temperature:TMP100"
    cap = _three_pad_c()
    symbols = {
        tmp: _tmp100_pins(), "Device:C": cap,
        "power:+3V3": _pwr("power:+3V3", "+3V3"),
        "power:GND": _pwr("power:GND", "GND"),
        "power:PWR_FLAG": SymbolDef(
            "power:PWR_FLAG", "",
            [PinDef("1", "pwr", PinType.PWROUT, 0, 0, 0, 2.54)],
            is_power=True, reference_prefix="#FLG",
        ),
    }
    ir = CircuitIR("three-pad")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+3V3", ("#PWR01", "1"), ("U1", "4"))
    ir.connect("GND", ("#PWR02", "1"), ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    ensure_pwr_flags(ir, symbols)
    assert capacitors_across_i2c_lines(ir, symbols)
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}
    issues = check_circuit(ir, symbols)
    assert not any(i.rule == "capacitor_across_i2c" for i in issues)
    assert not any(i.rule == "decoupling_missing" and "U1@" in i.path for i in issues)


def test_a_third_pad_on_the_bus_still_leaves_with_the_other_two():
    """Checker saw two nets; fixer used to noop because three pins sat on them."""
    from circuitgen.erc import capacitors_across_i2c_lines, check_circuit
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _three_pad_c()}
    ir = CircuitIR("three-on-bus")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.connect("+3V3", ("U1", "4"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"), ("C1", "3"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    assert capacitors_across_i2c_lines(ir, symbols)
    notes = detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}, notes
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert not any(r == "C1" for r, _ in sda.nodes)
    assert not any(r == "C1" for r, _ in scl.nodes)
    assert ("C1", "3") in ir.nc_pins
    assert not any(i.rule == "capacitor_across_i2c" for i in check_circuit(ir, symbols))


def test_two_named_sensors_on_different_rails_do_not_follow_net_order():
    """One C cannot be both bypasses; node order must not pick +3V3 vs +5V."""
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _two_pin_c()}

    def board(sda_u2_first: bool):
        ir = CircuitIR("two-tmp")
        ir.add(Component("U2", tmp, "TMP100"))
        ir.add(Component("U3", tmp, "TMP100"))
        ir.add(Component("C1", "Device:C", "0.01uF"))
        ir.connect("+3V3", ("U2", "4"))
        ir.connect("+5V", ("U3", "4"))
        ir.connect("GND", ("U2", "2"), ("U3", "2"))
        if sda_u2_first:
            ir.connect("SDA", ("U2", "6"), ("U3", "6"), ("C1", "1"))
        else:
            ir.connect("SDA", ("U3", "6"), ("U2", "6"), ("C1", "1"))
        ir.connect("SCL", ("U2", "1"), ("U3", "1"), ("C1", "2"))
        notes = detach_capacitors_across_i2c_lines(ir, symbols, None)
        on = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
        return on, {(r, p) for r, p in ir.nc_pins if r == "C1"}, notes

    a_on, a_nc, a_notes = board(True)
    b_on, b_nc, b_notes = board(False)
    assert a_on == b_on == set()
    assert a_nc == b_nc == {("C1", "1"), ("C1", "2")}
    assert any("do not share one supply/return pair" in n for n in a_notes)
    assert not any("named I2C devices" in n for n in a_notes + b_notes)
    assert not any("no supply/GND nets" in n for n in a_notes + b_notes)


def test_two_named_sensors_on_the_same_rail_keep_that_bypass():
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _two_pin_c()}

    def board(sda_u2_first: bool):
        ir = CircuitIR("two-tmp-same")
        ir.add(Component("U2", tmp, "TMP100"))
        ir.add(Component("U3", tmp, "TMP100"))
        ir.add(Component("C1", "Device:C", "0.01uF"))
        ir.connect("+3V3", ("U2", "4"), ("U3", "4"))
        ir.connect("GND", ("U2", "2"), ("U3", "2"))
        if sda_u2_first:
            ir.connect("SDA", ("U2", "6"), ("U3", "6"), ("C1", "1"))
        else:
            ir.connect("SDA", ("U3", "6"), ("U2", "6"), ("C1", "1"))
        ir.connect("SCL", ("U2", "1"), ("U3", "1"), ("C1", "2"))
        detach_capacitors_across_i2c_lines(ir, symbols, None)
        return {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}

    assert board(True) == board(False) == {"+3V3", "GND"}


def test_a_four_pin_shunt_is_not_an_i2c_pullup():
    from circuitgen.erc import two_pin_bridges
    from circuitgen.normalize import ensure_i2c_pullups

    tmp = "Sensor_Temperature:TMP100"
    shunt = SymbolDef(
        "Device:R_Shunt",
        "",
        [
            PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("2", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("3", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("4", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
        ],
        reference_prefix="R",
    )
    symbols = {
        tmp: _tmp100_pins(),
        "Device:R_Shunt": shunt,
        "Device:R": _two_pin_r(),
        "power:+3V3": _pwr("power:+3V3", "+3V3"),
    }
    ir = CircuitIR("shunt")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("RS1", "Device:R_Shunt", "0.01"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.connect("+3V3", ("#PWR01", "1"), ("U1", "4"), ("RS1", "2"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("RS1", "1"))
    ir.connect("KELVIN_A", ("RS1", "3"))
    ir.connect("KELVIN_B", ("RS1", "4"))
    ir.connect("SCL", ("U1", "1"))
    assert two_pin_bridges(ir, symbols, "R", "SDA") == []
    ensure_i2c_pullups(ir, symbols, "+3V3")
    added = [c for c in ir.components.values() if c.lib_id == "Device:R" and c.value == "10k"]
    assert added, "SDA still needs a 2-terminal pull-up"
    sda = next(n for n in ir.nets if n.name == "SDA")
    assert any(r for r, _ in sda.nodes if ir.components[r].lib_id == "Device:R")


def test_a_feedthrough_on_rail_gnd_and_sda_is_not_decoupling():
    from circuitgen.erc import capacitors_across_i2c_lines, two_pin_bridges

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _three_pad_c()}
    ir = CircuitIR("feed-decap")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "feed"))
    ir.connect("+3V3", ("U1", "4"), ("C1", "1"))
    ir.connect("GND", ("U1", "2"), ("C1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "3"))
    ir.connect("SCL", ("U1", "1"))
    assert capacitors_across_i2c_lines(ir, symbols) == []
    assert two_pin_bridges(ir, symbols, "C", "+3V3") == []


def test_a_feedthrough_across_sda_scl_and_a_rail_still_leaves_the_bus():
    from circuitgen.erc import capacitors_across_i2c_lines
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _three_pad_c()}
    ir = CircuitIR("feed-bus")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "feed"))
    ir.connect("+3V3", ("U1", "4"), ("C1", "3"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    assert capacitors_across_i2c_lines(ir, symbols)
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert not any(r == "C1" for r, _ in sda.nodes)
    assert not any(r == "C1" for r, _ in scl.nodes)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert "SDA" not in c1 and "SCL" not in c1
    assert "+3V3" in c1 and "GND" in c1


def _pwr_flag():
    return SymbolDef(
        "power:PWR_FLAG", "",
        [PinDef("1", "pwr", PinType.PWROUT, 0, 0, 0, 2.54)],
        is_power=True, reference_prefix="#FLG",
    )


def test_a_feedthrough_third_pad_on_another_rail_still_counts_as_bypass():
    """Pin 3 on +5V used to leave three nets; the note said bypass, ERC did not."""
    from circuitgen.erc import capacitors_across_i2c_lines, check_circuit, two_pin_bridges
    from circuitgen.normalize import detach_capacitors_across_i2c_lines, ensure_pwr_flags

    tmp = "Sensor_Temperature:TMP100"
    symbols = {
        tmp: _tmp100_pins(),
        "Device:C": _three_pad_c(),
        "power:+3V3": _pwr("power:+3V3", "+3V3"),
        "power:+5V": _pwr("power:+5V", "+5V"),
        "power:GND": _pwr("power:GND", "GND"),
        "power:PWR_FLAG": _pwr_flag(),
    }
    ir = CircuitIR("feed-other-rail")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "feed"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.add(Component("#PWR03", "power:+5V", "+5V"))
    ir.connect("+3V3", ("#PWR01", "1"), ("U1", "4"))
    ir.connect("GND", ("#PWR02", "1"), ("U1", "2"))
    ir.connect("+5V", ("#PWR03", "1"), ("C1", "3"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    ensure_pwr_flags(ir, symbols)
    assert capacitors_across_i2c_lines(ir, symbols)
    notes = detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}, notes
    assert ("C1", "3") in ir.nc_pins
    assert set(two_pin_bridges(ir, symbols, "C", "+3V3")) == {"GND"}
    issues = check_circuit(ir, symbols)
    assert not any(i.rule == "capacitor_across_i2c" for i in issues)
    assert not any(i.rule == "decoupling_missing" and "U1@" in i.path for i in issues)


def test_prefix_c_lowercase_still_counts_as_decoupling_after_the_move():
    from circuitgen.erc import check_circuit, two_pin_bridges
    from circuitgen.normalize import detach_capacitors_across_i2c_lines, ensure_pwr_flags

    tmp = "Sensor_Temperature:TMP100"
    cap = SymbolDef(
        "Device:C", "",
        [
            PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54),
            PinDef("2", "~", PinType.PASSIVE, 0, 0, 180, 2.54),
        ],
        reference_prefix="c",
    )
    symbols = {
        tmp: _tmp100_pins(), "Device:C": cap,
        "power:+3V3": _pwr("power:+3V3", "+3V3"),
        "power:GND": _pwr("power:GND", "GND"),
        "power:PWR_FLAG": _pwr_flag(),
    }
    ir = CircuitIR("prefix-c")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "0.01uF"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+3V3", ("#PWR01", "1"), ("U1", "4"))
    ir.connect("GND", ("#PWR02", "1"), ("U1", "2"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    ensure_pwr_flags(ir, symbols)
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    assert set(two_pin_bridges(ir, symbols, "C", "+3V3")) == {"GND"}
    issues = check_circuit(ir, symbols)
    assert not any(i.rule == "decoupling_missing" and "U1@" in i.path for i in issues)


def test_an_integer_pin_number_on_a_foreign_rail_is_still_ncd():
    from circuitgen.erc import two_pin_bridges
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _tmp100_pins(), "Device:C": _three_pad_c()}
    ir = CircuitIR("int-pin")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("C1", "Device:C", "feed"))
    ir.connect("+3V3", ("U1", "4"))
    ir.connect("GND", ("U1", "2"))
    ir.connect("+5V", ("C1", 3))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("SCL", ("U1", "1"), ("C1", "2"))
    detach_capacitors_across_i2c_lines(ir, symbols, None)
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"+3V3", "GND"}
    assert not any(n.name == "+5V" and any(r == "C1" for r, _ in n.nodes) for n in ir.nets)
    assert any(r == "C1" and str(p) == "3" for r, p in ir.nc_pins)
    assert set(two_pin_bridges(ir, symbols, "C", "+3V3")) == {"GND"}


def test_a_timing_capacitor_on_nets_named_sda_scl_is_not_moved():
    from circuitgen.erc import capacitors_across_i2c_lines, is_i2c_net
    from circuitgen.normalize import detach_capacitors_across_i2c_lines

    timer = "Timer:NE555"
    symbols = {
        timer: _sym(timer, [
            (1, "GND", PinType.PWRIN), (6, "THRES", PinType.INPUT),
            (7, "DISCH", PinType.OUTPUT), (8, "VCC", PinType.PWRIN),
        ]),
        "Device:C": _two_pin_c(),
    }
    ir = CircuitIR("555-labels")
    ir.add(Component("U1", timer, "NE555"))
    ir.add(Component("C1", "Device:C", "10nF"))
    ir.connect("SDA", ("U1", "7"), ("C1", "1"))
    ir.connect("SCL", ("U1", "6"), ("C1", "2"))
    ir.connect("VCC", ("U1", "8"))
    ir.connect("GND", ("U1", "1"))
    assert capacitors_across_i2c_lines(ir, symbols) == []
    assert is_i2c_net(ir, symbols, next(n for n in ir.nets if n.name == "SDA"))
    assert detach_capacitors_across_i2c_lines(ir, symbols, "VCC") == []
    c1 = {n.name for n in ir.nets if any(r == "C1" for r, _ in n.nodes)}
    assert c1 == {"SDA", "SCL"}


def test_a_capacitor_from_sda_to_gnd_is_not_across_the_bus():
    from circuitgen.erc import capacitors_across_i2c_lines
    from circuitgen.normalize import detach_capacitors_across_i2c_lines
    from circuitgen.symbols import load_symbols

    sensor = "Sensor_Temperature:TMP100"
    symbols = load_symbols([sensor, "Device:C", "power:GND"])
    ir = CircuitIR("filter")
    ir.add(Component("U1", sensor, "TMP100"))
    ir.add(Component("C1", "Device:C", "100pF"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("SDA", ("U1", "6"), ("C1", "1"))
    ir.connect("GND", ("#PWR02", "1"), ("U1", "2"), ("C1", "2"))
    assert capacitors_across_i2c_lines(ir, symbols) == []
    assert detach_capacitors_across_i2c_lines(ir, symbols, "+3V3") == []
    assert ("C1", "1") in next(n for n in ir.nets if n.name == "SDA").nodes
