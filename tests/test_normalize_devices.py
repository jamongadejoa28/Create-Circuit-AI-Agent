import pytest

from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import (
    add_shared_spi_miso_series_resistors,
    apply_stm32g474ret6_foc_pinmap,
    complete_known_device_pins,
    ensure_drv8311_vm_decoupling,
    ensure_drv8311h_operating_network,
    ensure_dc_power_entry,
    ensure_canfd_bus_protection,
    enforce_requested_stm32_variant,
    ensure_stm32g4_power_network,
    ensure_stm32g4_system_support,
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


def test_drv8311h_three_pwm_support_and_motor_connector():
    lid = "Driver_Motor:DRV8311H"
    pin_rows = [
        (1, "~{FAULT}", PinType.OPENCOLL), (2, "CSAREF", PinType.INPUT),
        (3, "SOC", PinType.OUTPUT), (4, "SOB", PinType.OUTPUT),
        (5, "SOA", PinType.OUTPUT), (6, "CP", PinType.INPUT),
        (7, "VIN_AVDD", PinType.PWRIN), (8, "VM", PinType.PWRIN),
        (9, "PGND", PinType.PWRIN), (10, "OUTA", PinType.TRISTATE),
        (11, "OUTB", PinType.TRISTATE), (12, "OUTC", PinType.TRISTATE),
        (13, "INHC", PinType.INPUT), (14, "INHB", PinType.INPUT),
        (15, "INHA", PinType.INPUT), (16, "AGND", PinType.PWRIN),
        (17, "AVDD", PinType.PWROUT), (18, "INLA", PinType.INPUT),
        (19, "INLB", PinType.INPUT), (20, "INLC", PinType.INPUT),
        (21, "GAIN", PinType.INPUT), (22, "SLEW", PinType.INPUT),
        (23, "MODE", PinType.INPUT), (24, "~{SLEEP}", PinType.INPUT),
    ]
    ir = CircuitIR("drv")
    ir.add(Component("U1", lid, "DRV8311H", group="MOTOR1"))
    ir.connect("+12V", ("U1", "8"), ("U1", "7"))
    ir.connect("GND", ("U1", "9"), ("U1", "16"))
    ir.connect("EN_WRONG", ("U1", "19"))
    notes = ensure_drv8311h_operating_network(ir, {lid: _sym(lid, pin_rows)})
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert ("U1", "19") in by_net["GND"] and "EN_WRONG" not in by_net
    assert ("U1", "24") in by_net["+3V3"]
    assert ("U1", "23") in ir.nc_pins
    assert {"M1_OUTA", "M1_OUTB", "M1_OUTC"} <= set(by_net)
    assert any(c.lib_id == "Connector_Generic:Conn_01x03" for c in ir.components.values())
    assert {c.value for c in ir.components.values()} >= {"100nF", "1uF", "5.1k"}
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


def test_explicit_stm32g474ret6_request_migrates_smaller_variant_by_pin_name():
    old_id = "MCU_ST_STM32G4:STM32G474CBTx"
    old = _sym(old_id, [
        (8, "PA0", PinType.BIDIR), (13, "PA5", PinType.BIDIR),
        (24, "VDD", PinType.PWRIN), (23, "VSS", PinType.PWRIN),
    ])
    ir = CircuitIR("variant")
    ir.add(Component("U1", old_id, "STM32G474CBTx"))
    ir.connect("PWM", ("U1", "13"))
    ir.connect("+3V3", ("U1", "24"))
    notes = enforce_requested_stm32_variant(ir, "STM32G474RET6 board", {old_id: old})
    assert ir.components["U1"].lib_id == "MCU_ST_STM32G4:STM32G474RETx"
    assert ir.components["U1"].value == "STM32G474RET6"
    # LQFP64 PA5=19 and first VDD=16 in the official KiCad symbol.
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert ("U1", "19") in by_net["PWM"]
    assert ("U1", "16") in by_net["+3V3"]
    assert notes


def test_ret6_foc_pinmap_assigns_12_pwm_12_adc_spi_and_fdcan_without_conflicts():
    from circuitgen.symbols import load_symbols

    mcu_lid = "MCU_ST_STM32G4:STM32G474RETx"
    drv_lid = "Driver_Motor:DRV8311H"
    enc_lid = "Sensor_Magnetic:AS5048A"
    symbols = load_symbols([mcu_lid, drv_lid, enc_lid])
    ir = CircuitIR("foc_map")
    ir.add(Component("U1", mcu_lid, "STM32G474RET6", group="MCU"))
    for channel in range(1, 5):
        ir.add(Component(f"U{channel + 1}", drv_lid, "DRV8311H", group=f"MOTOR{channel}"))
        ir.add(Component(f"U{channel + 5}", enc_lid, "AS5048A", group=f"ENC{channel}"))
    notes = apply_stm32g474ret6_foc_pinmap(ir, symbols)
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert all(f"PWM_{phase}{channel}" in by_net for channel in range(1, 5) for phase in "ABC")
    assert all(f"M{channel}_SO{phase}_ADC" in by_net for channel in range(1, 5) for phase in "ABC")
    assert {c.value for c in ir.components.values()}.issuperset({"47R", "1nF"})
    assert sum(c.value == "47R" for c in ir.components.values()) == 12
    assert sum(c.value == "1nF" for c in ir.components.values()) == 12
    assert {("U1", "58")} <= by_net["CAN_RX"]  # PB5 / FDCAN2_RX
    assert {("U1", "59")} <= by_net["CAN_TX"]  # PB6 / FDCAN2_TX
    assert {("U1", "19")} <= by_net["SPI_SCK"]  # PA5 / SPI1_SCK
    owners = {}
    for net in ir.nets:
        for node in net.nodes:
            owners.setdefault(node, set()).add(net.name)
    assert all(len(names) == 1 for names in owners.values())
    assert notes


def test_canfd_connector_selectable_termination_and_tvs_are_added_once():
    ir = CircuitIR("can")
    ir.add(Component("U1", "Interface_CAN_LIN:TJA1051T", "TJA1051T", group="MCU"))
    notes = ensure_canfd_bus_protection(ir)
    assert len(notes) == 1
    assert sum(c.value == "CAN_FD" for c in ir.components.values()) == 1
    assert sum(c.value == "120R" for c in ir.components.values()) == 1
    assert sum(c.value == "CAN_TERM_ENABLE" for c in ir.components.values()) == 1
    assert sum(c.value == "CAN_ESD_TVS" for c in ir.components.values()) == 2
    assert ensure_canfd_bus_protection(ir) == []
    by_net = {n.name: set(n.nodes) for n in ir.nets}
    assert {"CANH", "CANL", "CAN_TERM", "GND"} <= set(by_net)


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


def test_shared_encoder_miso_gets_one_series_resistor_per_encoder():
    lid = "Sensor_Magnetic:AS5048A"
    symbols = {lid: _sym(lid, [(3, "MISO", PinType.OUTPUT)])}
    ir = CircuitIR("x")
    ir.add(Component("U1", lid, "AS5048A", group="ENC1"))
    ir.add(Component("U2", lid, "AS5048A", group="ENC2"))
    ir.connect("SPI_MISO", ("U1", "3"), ("U2", "3"))
    notes = add_shared_spi_miso_series_resistors(ir, symbols)
    assert len(notes) == 2
    assert {r for r in ir.components if r.startswith("R")} == {"R1", "R2"}
    shared = next(n for n in ir.nets if n.name == "SPI_MISO")
    assert ("U1", "3") not in shared.nodes and ("U2", "3") not in shared.nodes
    assert {"ENC1_MISO_RAW", "ENC2_MISO_RAW"} <= {n.name for n in ir.nets}


def test_shared_encoder_do_connects_missing_channel_and_isolates_all():
    lid = "Sensor_Magnetic:AS5045B"
    symbols = {lid: _sym(lid, [(9, "DO", PinType.OUTPUT)])}
    ir = CircuitIR("x")
    for i in range(1, 4):
        ir.add(Component(f"U{i}", lid, "AS5045B", group=f"ENCODER{i}"))
    ir.connect("SPI_MISO", ("U1", "9"), ("U2", "9"))
    notes = add_shared_spi_miso_series_resistors(ir, symbols)
    assert len(notes) == 3
    shared = next(n for n in ir.nets if n.name == "SPI_MISO")
    assert not any(ref.startswith("U") for ref, _ in shared.nodes)
    assert {"ENCODER1_MISO_RAW", "ENCODER2_MISO_RAW", "ENCODER3_MISO_RAW"} <= {
        n.name for n in ir.nets
    }


def test_driver_gets_complete_vm_decoupling_set_in_its_group():
    lid = "Driver_Motor:DRV8311H"
    symbols = {lid: _sym(lid, [
        (8, "VM", PinType.PWRIN), (9, "PGND", PinType.PWRIN)
    ])}
    ir = CircuitIR("x")
    ir.add(Component("U1", lid, "DRV8311H", group="MOTOR1"))
    ir.connect("VBAT", ("U1", "8"))
    ir.connect("GND", ("U1", "9"))
    notes = ensure_drv8311_vm_decoupling(ir, symbols)
    assert len(notes) == 4
    assert {c.value for c in ir.components.values() if c.lib_id == "Device:C"} == {
        "100nF", "1uF", "10uF", "220uF"
    }
    assert all(c.group == "MOTOR1" for c in ir.components.values())


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
    notes = ensure_stm32g4_power_network(ir, symbols)
    caps = [c for c in ir.components.values() if c.lib_id == "Device:C"]
    assert [c.value for c in caps].count("100nF") == 3  # two VDD + one VDDA
    assert {c.value for c in caps} >= {"10uF", "1uF"}
    assert any(c.lib_id == "Device:FerriteBead" for c in ir.components.values())
    analog = next(n for n in ir.nets if n.name == "MCU_VDDA")
    assert {("U1", "28"), ("U1", "29")} <= set(analog.nodes)


def test_stm32g4_system_aliases_create_reset_boot_and_swd_support():
    lid = "MCU_ST_STM32G4:STM32G474RETx"
    symbols = {lid: _sym(lid, [
        (7, "PG10", PinType.BIDIR), (49, "PA13", PinType.BIDIR),
        (50, "PA14", PinType.BIDIR), (56, "PB3", PinType.BIDIR),
        (61, "PB8", PinType.BIDIR),
    ])}
    ir = CircuitIR("x")
    ir.add(Component("U1", lid, "STM32G474", group="MCU"))
    ir.connect("+3V3", ("U1", "7"))  # deliberately wrong prior model wiring
    ir.connect("SPI_SCK", ("U1", "50"))
    ir.connect("GND")
    notes = ensure_stm32g4_system_support(ir, symbols)
    nodes = {n.name: set(n.nodes) for n in ir.nets}
    assert ("U1", "7") in nodes["NRST"]
    assert ("U1", "61") in nodes["BOOT0"]
    assert ("U1", "49") in nodes["SWDIO"]
    assert ("U1", "50") in nodes["SWCLK"] and ("U1", "50") not in nodes.get("SPI_SCK", set())
    assert any(c.value == "ARM_SWD_10PIN" for c in ir.components.values())
    assert any(c.value == "BOOT_MODE" for c in ir.components.values())
    assert any("NRST protection" in note for note in notes)


def test_merge_dangling_interface_nets():
    from circuitgen.normalize import merge_dangling_interface_nets

    ir = CircuitIR("m")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "MCU"))
    ir.add(Component("U2", "Sensor_Magnetic:AS5048A", "ENC"))
    ir.connect("SPI_MOSI", ("U1", "21"))
    ir.connect("MOSI", ("U2", "4"))
    ir.connect("ENC1_CS", ("U1", "22"), ("U2", "5"))
    notes = merge_dangling_interface_nets(ir)
    assert any("MOSI" in n for n in notes)
    names = [n.name for n in ir.nets]
    assert "MOSI" not in names and "SPI_MOSI" in names
    spi = next(n for n in ir.nets if n.name == "SPI_MOSI")
    assert ("U2", "4") in [(r, p) for r, p in spi.nodes]
    # untouched: already-connected net
    assert len(next(n for n in ir.nets if n.name == "ENC1_CS").nodes) == 2


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
