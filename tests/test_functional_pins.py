"""Functional pin completeness gate — IR layer A connectivity."""

from circuitgen.erc import check_circuit
from circuitgen.functional_pins import check_functional_pin_completeness
from circuitgen.ir import CircuitIR, Component, InterfaceContract, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib_id, pins, n_pins=None):
    plist = [
        PinDef(str(no), name, typ, 0, 0, 0, 2.54)
        for no, name, typ in pins
    ]
    return SymbolDef(lib_id, "", plist, reference_prefix="U")


def test_dangling_sda_is_error_unbound():
    tmp = "Sensor_Temperature:TMP100"
    symbols = {
        tmp: _sym(tmp, [
            (1, "SCL", PinType.BIDIR),
            (2, "GND", PinType.PWRIN),
            (4, "V+", PinType.PWRIN),
            (6, "SDA", PinType.BIDIR),
        ]),
    }
    ir = CircuitIR("dangle")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.connect("SCL", ("U1", "1"))
    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_pin_unbound" and "SDA" in i.message for i in issues)


def test_dangling_spi_and_uart_pins_are_errors():
    dev = "Interface:SerialPeripheral"
    symbols = {dev: _sym(dev, [
        (1, "SCK", PinType.INPUT),
        (2, "TXD", PinType.OUTPUT),
    ])}
    ir = CircuitIR("serial-dangle")
    ir.add(Component("U1", dev, "serial peripheral"))

    issues = check_functional_pin_completeness(ir, symbols)
    messages = [i.message for i in issues if i.rule == "functional_pin_unbound"]
    assert any("SPI_SCK" in message for message in messages)
    assert any("SERIAL_TX" in message for message in messages)


def test_sda_on_net_passes():
    tmp = "Sensor_Temperature:TMP100"
    symbols = {
        tmp: _sym(tmp, [
            (1, "SCL", PinType.BIDIR),
            (6, "SDA", PinType.BIDIR),
        ]),
    }
    ir = CircuitIR("ok")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.connect("SDA", ("U1", "6"))
    ir.connect("SCL", ("U1", "1"))
    issues = check_functional_pin_completeness(ir, symbols)
    assert not any(i.rule == "functional_pin_unbound" for i in issues)
    assert any(i.rule == "functional_controller_missing" for i in issues)


def test_peripheral_functional_pin_marked_nc_is_error():
    tmp = "Sensor_Temperature:TMP100"
    symbols = {tmp: _sym(tmp, [(1, "SCL", PinType.BIDIR)])}
    ir = CircuitIR("peripheral-nc")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.nc_pins.append(("U1", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_pin_marked_nc" for i in issues)


def test_unused_hub_interface_pin_may_be_explicit_nc():
    mcu = "RF_Module:ESP32-WROOM-32"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu])
    ir = CircuitIR("hub-nc")
    ir.add(Component("U1", mcu, "ESP32-WROOM-32"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.nc_pins.append(("U1", "20"))  # SCK/CLK, unused module interface

    assert not any(
        i.rule == "functional_pin_marked_nc"
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_i2c_net_without_hub_is_error():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    tmp = "Sensor_Temperature:TMP100"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu, tmp])
    ir = CircuitIR("no-hub")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", tmp, "TMP100"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.connect("SDA", ("U2", "6"))
    ir.connect("SCL", ("U2", "1"))
    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_bus_missing_controller" for i in issues)
    ir.connect("SDA", ("U1", "50"))
    ir.connect("SCL", ("U1", "49"))
    assert not any(i.rule == "functional_bus_missing_controller" for i in check_functional_pin_completeness(ir, symbols))


def test_spi_net_without_hub_is_error():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    spi = "Interface_SPI:Peripheral"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu])
    symbols[spi] = _sym(spi, [(1, "SCK", PinType.INPUT)])
    ir = CircuitIR("spi-no-hub")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", spi, "SPI peripheral"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.connect("SPI_SCK", ("U2", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(
        i.rule == "functional_bus_missing_controller"
        and "SPI" in i.message
        and "SCK" in i.message
        for i in issues
    )


def test_uart_net_without_hub_is_error():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    uart = "Interface_UART:Peripheral"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu])
    symbols[uart] = _sym(uart, [(1, "TXD", PinType.OUTPUT)])
    ir = CircuitIR("uart-no-hub")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", uart, "UART peripheral"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.interface_contracts.append(InterfaceContract(
        "UART_RX", peer="controller", protocol="uart"
    ))
    ir.connect("UART_RX", ("U2", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(
        i.rule == "functional_bus_missing_controller"
        and "UART" in i.message
        and "TX" in i.message
        for i in issues
    )

    # PA10 is a recorded USART1_RX pin for this package.
    ir.connect("UART_RX", ("U1", "44"))
    assert not any(
        i.rule == "functional_bus_missing_controller"
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_uart_label_without_a_uart_named_pin_is_not_bus_evidence():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    peripheral = "Interface:Generic"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu])
    symbols[peripheral] = _sym(peripheral, [(1, "DATA", PinType.BIDIR)])
    ir = CircuitIR("uart-label-only")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", peripheral, "generic"))
    ir.connect("UART_TX", ("U2", "1"))

    assert not any(
        i.rule == "functional_bus_missing_controller"
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_tmp100_add_nc_is_not_unbound_error():
    tmp = "Sensor_Temperature:TMP100"
    symbols = {
        tmp: _sym(tmp, [
            (1, "SCL", PinType.BIDIR),
            (3, "ADD1", PinType.INPUT),
            (5, "ADD0", PinType.INPUT),
            (6, "SDA", PinType.BIDIR),
        ]),
    }
    ir = CircuitIR("add")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.connect("SDA", ("U1", "6"))
    ir.connect("SCL", ("U1", "1"))
    ir.nc_pins.extend([("U1", "3"), ("U1", "5")])
    issues = check_functional_pin_completeness(ir, symbols)
    assert not any(i.rule == "functional_pin_unbound" for i in issues)


def test_i2c_peripherals_without_any_controller_is_error():
    tmp = "Sensor_Temperature:TMP100"
    resistor = "Device:R"
    symbols = {
        tmp: _sym(tmp, [(1, "SCL", PinType.BIDIR), (6, "SDA", PinType.BIDIR)]),
        resistor: _sym(resistor, [(1, "~", PinType.PASSIVE), (2, "~", PinType.PASSIVE)]),
    }
    ir = CircuitIR("two-sensors-no-controller")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.add(Component("U2", tmp, "TMP100"))
    ir.add(Component("R1", resistor, "4.7k"))
    ir.add(Component("R2", resistor, "4.7k"))
    ir.connect("SDA", ("U1", "6"), ("U2", "6"), ("R1", "1"))
    ir.connect("SCL", ("U1", "1"), ("U2", "1"), ("R2", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_controller_missing" for i in issues)


def test_large_connector_cannot_impersonate_controller():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    tmp = "Sensor_Temperature:TMP100"
    connector = "Connector_Generic:Conn_01x20"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu, tmp, connector])
    ir = CircuitIR("connector-is-not-controller")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", tmp, "TMP100"))
    ir.add(Component("J1", connector, "header"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.connect("SDA", ("U2", "6"), ("J1", "1"))
    ir.connect("SCL", ("U2", "1"), ("J1", "2"))

    issues = check_functional_pin_completeness(ir, symbols)
    missing = [i for i in issues if i.rule == "functional_bus_missing_controller"]
    assert {i.path for i in missing} == {"net:SDA", "net:SCL"}


def test_generic_control_contract_must_reach_declared_controller():
    symbols = {
        "Controller:Small": _sym("Controller:Small", [(1, "GPIO", PinType.BIDIR)]),
        "Driver:Motor": _sym("Driver:Motor", [(1, "PWM", PinType.INPUT)]),
        "Connector:Header": _sym("Connector:Header", [(1, "Pin_1", PinType.PASSIVE)]),
    }
    ir = CircuitIR("generic-control")
    ir.add(Component("U1", "Controller:Small", "controller", group="MCU"))
    ir.add(Component("U2", "Driver:Motor", "driver", group="DRIVER"))
    ir.add(Component("J1", "Connector:Header", "header", group="DRIVER"))
    ir.controller_required = True
    ir.controller_refs = ["U1"]
    ir.interface_contracts.append(InterfaceContract(
        "MOTOR_PWM", owner_group="DRIVER", peer="controller",
        protocol="generic_control", purpose="motor command",
    ))
    ir.connect("MOTOR_PWM", ("U2", "1"), ("J1", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_interface_missing_peer" for i in issues)


def test_block_and_external_contracts_require_their_typed_peer():
    symbols = {
        "Logic:Owner": _sym("Logic:Owner", [(1, "A", PinType.BIDIR), (2, "B", PinType.BIDIR)]),
        "Logic:Peer": _sym("Logic:Peer", [(1, "A", PinType.BIDIR)]),
        "Connector_Generic:Conn_01x01": _sym(
            "Connector_Generic:Conn_01x01", [(1, "Pin_1", PinType.PASSIVE)]
        ),
    }
    ir = CircuitIR("typed-peers")
    ir.add(Component("U1", "Logic:Owner", "owner", group="OWNER"))
    ir.add(Component("U2", "Logic:Peer", "peer", group="PEER"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x01", "port", group="OWNER"))
    ir.controller_required = False
    ir.interface_contracts.extend([
        InterfaceContract("TO_BLOCK", "OWNER", "block", "other"),
        InterfaceContract("TO_PORT", "OWNER", "external", "other"),
    ])
    ir.connect("TO_BLOCK", ("U1", "1"))
    ir.connect("TO_PORT", ("U1", "2"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert sum(i.rule == "functional_interface_missing_peer" for i in issues) == 2

    ir.connect("TO_BLOCK", ("U2", "1"))
    ir.connect("TO_PORT", ("J1", "1"))
    assert not any(
        i.rule.startswith("functional_interface_")
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_active_spi_chip_select_marked_nc_is_error():
    symbols = {
        "Controller:Small": _sym("Controller:Small", [
            (1, "GPIO1", PinType.BIDIR), (2, "GPIO2", PinType.BIDIR),
            (3, "GPIO3", PinType.BIDIR),
        ]),
        "Converter:SPI": _sym("Converter:SPI", [
            (1, "SCK", PinType.INPUT), (2, "MOSI", PinType.INPUT),
            (3, "MISO", PinType.OUTPUT), (4, "~{CS}", PinType.INPUT),
        ]),
    }
    ir = CircuitIR("spi-cs-nc")
    ir.add(Component("U1", "Controller:Small", "controller"))
    ir.add(Component("U2", "Converter:SPI", "converter"))
    ir.controller_refs = ["U1"]
    ir.connect("SCK", ("U1", "1"), ("U2", "1"))
    ir.connect("MOSI", ("U1", "2"), ("U2", "2"))
    ir.connect("MISO", ("U1", "3"), ("U2", "3"))
    ir.nc_pins.append(("U2", "4"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(
        i.rule == "functional_pin_marked_nc" and "SPI_NSS" in i.message
        for i in issues
    )


def test_unrelated_cs_pin_is_not_promoted_to_spi():
    analog = "Amplifier_Instrumentation:AD8231"
    symbols = {analog: _sym(analog, [(1, "CS", PinType.INPUT), (2, "OUT", PinType.OUTPUT)])}
    ir = CircuitIR("analog-cs")
    ir.add(Component("U1", analog, "AD8231"))
    ir.nc_pins.append(("U1", "1"))

    assert not any(
        i.rule == "functional_pin_marked_nc"
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_uart_controller_pin_must_have_recorded_uart_af():
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    uart = "Interface_UART:Peripheral"
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    symbols = parts.load_symbols([mcu])
    symbols[uart] = _sym(uart, [(1, "TXD", PinType.OUTPUT)])
    ir = CircuitIR("uart-wrong-af")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", uart, "UART peripheral"))
    ir.controller_refs = ["U1"]
    ir.interface_contracts.append(InterfaceContract(
        "UART_RX", peer="controller", protocol="uart"
    ))
    ir.connect("UART_RX", ("U2", "1"), ("U1", "2"))  # PC13, not UART RX

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_controller_af_mismatch" for i in issues)


def test_can_txd_is_not_reported_as_uart():
    can = "Interface_CAN_LIN:TJA1051T"
    symbols = {can: _sym(can, [(1, "TXD", PinType.INPUT), (4, "RXD", PinType.OUTPUT)])}
    ir = CircuitIR("can-context")
    ir.add(Component("U1", can, "TJA1051T"))

    messages = [
        issue.message for issue in check_functional_pin_completeness(ir, symbols)
        if issue.rule == "functional_pin_unbound"
    ]
    assert any("CAN_TX" in message for message in messages)
    assert all("UART" not in message for message in messages)


def test_check_circuit_includes_functional_unbound():
    tmp = "Sensor_Temperature:TMP100"
    symbols = {
        tmp: _sym(tmp, [
            (1, "SCL", PinType.BIDIR),
            (6, "SDA", PinType.BIDIR),
        ]),
    }
    ir = CircuitIR("erc")
    ir.add(Component("U1", tmp, "TMP100"))
    ir.connect("SCL", ("U1", "1"))
    issues = check_circuit(ir, symbols)
    assert any(i.rule == "functional_pin_unbound" for i in issues)
