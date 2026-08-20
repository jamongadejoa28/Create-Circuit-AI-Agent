"""Functional pin completeness gate — IR layer A connectivity."""

from circuitgen.erc import check_circuit
from circuitgen.functional_pins import check_functional_pin_completeness
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
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
    assert any("UART_TX" in message for message in messages)


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
    assert check_functional_pin_completeness(ir, symbols) == []


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
    ir.connect("SDA", ("U2", "6"))
    ir.connect("SCL", ("U2", "1"))
    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_bus_missing_hub" for i in issues)
    ir.connect("SDA", ("U1", "50"))
    ir.connect("SCL", ("U1", "49"))
    assert not any(i.rule == "functional_bus_missing_hub" for i in check_functional_pin_completeness(ir, symbols))


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
    ir.connect("SPI_SCK", ("U2", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(
        i.rule == "functional_bus_missing_hub"
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
    ir.connect("UART_RX", ("U2", "1"))

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(
        i.rule == "functional_bus_missing_hub"
        and "UART" in i.message
        and "TX" in i.message
        for i in issues
    )

    # PA10 is a recorded USART1_RX pin for this package.  The functional
    # completeness gate only needs the controller to reach the net; AF
    # correctness remains a separate datasheet-backed concern.
    ir.connect("UART_RX", ("U1", "44"))
    assert not any(
        i.rule == "functional_bus_missing_hub"
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
        i.rule == "functional_bus_missing_hub"
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
    assert check_functional_pin_completeness(ir, symbols) == []


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
