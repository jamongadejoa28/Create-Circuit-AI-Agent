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
