from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import (
    add_shared_spi_miso_series_resistors,
    complete_known_device_pins,
    ensure_drv8311_vm_decoupling,
)
from circuitgen.pins import PinType


def _sym(lib_id, pins):
    return SymbolDef(
        lib_id, "", [PinDef(str(no), name, typ, 0, 0, 0, 2.54) for no, name, typ in pins]
    )


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
