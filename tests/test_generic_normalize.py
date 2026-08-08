from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_bus_pullups, ensure_relay_flyback, mark_documented_no_connects
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


def test_i2c_pullup_reuses_miswired_resistor_instead_of_duplicating_it():
    rlib, slib = "Device:R", "Sensor:I2C"
    symbols = {
        rlib: SymbolDef(rlib, "", [PinDef("1", "~", PinType.PASSIVE, 0, 0, 0, 2.54), PinDef("2", "~", PinType.PASSIVE, 0, 0, 0, 2.54)], reference_prefix="R"),
        slib: SymbolDef(slib, "", [PinDef("1", "SDA", PinType.BIDIR, 0, 0, 0, 2.54)]),
    }
    ir = CircuitIR("pullup")
    ir.add(Component("U1", slib, "SENSOR")); ir.add(Component("R1", rlib, "4.7k"))
    ir.connect("SDA", ("U1", "1")); ir.connect("GND", ("R1", "2")); ir.connect("+3V3")
    notes = ensure_bus_pullups(ir, symbols, "+3V3")
    assert notes == ["rewired R1 as I2C pull-up on SDA to +3V3"]
    assert set(next(n.nodes for n in ir.nets if n.name == "SDA")) == {("U1", "1"), ("R1", "1")}
    assert next(n.nodes for n in ir.nets if n.name == "+3V3") == [("R1", "2")]


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
