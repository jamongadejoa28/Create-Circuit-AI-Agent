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
