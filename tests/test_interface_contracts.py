"""RequirementSpec → InterfaceContract — size-independent correctness model."""

from __future__ import annotations

from circuitgen.blocks import validate_plan
from circuitgen.functional_pins import check_functional_pin_completeness
from circuitgen.interface_contracts import (
    apply_spec_interface_contracts,
    interface_contracts_from_spec,
    merge_interface_contracts,
    routing_net_priority,
)
from circuitgen.ir import CircuitIR, Component, InterfaceContract, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib_id: str, pins: list[tuple]) -> SymbolDef:
    plist = [
        PinDef(str(no), name, etype, 0, 0, 0, 2.54)
        for no, name, etype in pins
    ]
    return SymbolDef(lib_id, "", plist, reference_prefix="U")


def test_interface_contracts_from_spec_defaults_and_typing():
    contracts = interface_contracts_from_spec({
        "signals": [
            {"name": "TX", "purpose": "uart out"},
            {
                "name": "MOTOR_PWM",
                "purpose": "speed command",
                "peer": "controller",
                "protocol": "generic_control",
                "required": True,
                "owner_role": "motor_driver",
            },
            {"name": "SDA", "peer": "controller", "protocol": "i2c"},
            {"name": ""},
            {"name": "MOTOR_PWM"},  # duplicate ignored
        ],
    })
    by_net = {c.net: c for c in contracts}
    assert set(by_net) == {"TX", "MOTOR_PWM", "SDA"}
    assert by_net["TX"].peer == "external"
    assert by_net["TX"].protocol == "other"
    assert by_net["TX"].required is True
    assert by_net["MOTOR_PWM"].peer == "controller"
    assert by_net["MOTOR_PWM"].protocol == "generic_control"
    assert by_net["SDA"].protocol == "i2c"


def test_merge_prefers_owner_group_from_block_instantiation():
    floor = interface_contracts_from_spec({
        "signals": [{
            "name": "MOTOR_PWM",
            "peer": "controller",
            "protocol": "generic_control",
        }],
    })
    from_plan = [InterfaceContract(
        "MOTOR_PWM", owner_group="DRIVER1", peer="controller",
        protocol="generic_control",
    )]
    merged = merge_interface_contracts(floor, from_plan)
    assert len(merged) == 1
    assert merged[0].owner_group == "DRIVER1"


def test_flat_spec_contracts_gate_generic_control_without_block_plan():
    """parts_needed < BLOCK_THRESHOLD never ran instantiate_blocks.

    Spec-authored contracts must still make PWM reach the declared controller.
    """
    symbols = {
        "Controller:Small": _sym("Controller:Small", [(1, "GPIO", PinType.BIDIR)]),
        "Driver:Motor": _sym("Driver:Motor", [(1, "PWM", PinType.INPUT)]),
        "Connector:Header": _sym("Connector:Header", [(1, "Pin_1", PinType.PASSIVE)]),
    }
    ir = CircuitIR("flat-pwm")
    ir.add(Component("U1", "Controller:Small", "controller"))
    ir.add(Component("U2", "Driver:Motor", "driver"))
    ir.add(Component("J1", "Connector:Header", "header"))
    ir.connect("MOTOR_PWM", ("U2", "1"), ("J1", "1"))

    spec = {
        "parts_needed": [
            {"role": "mcu", "search_query": "MCU", "functional_kind": "microcontroller"},
            {"role": "motor_driver", "search_query": "driver", "functional_kind": "motor_driver"},
        ],
        "signals": [{
            "name": "MOTOR_PWM",
            "peer": "controller",
            "protocol": "generic_control",
            "required": True,
            "owner_role": "motor_driver",
        }],
    }
    apply_spec_interface_contracts(ir, spec)
    ir.controller_required = True
    ir.controller_refs = ["U1"]

    issues = check_functional_pin_completeness(ir, symbols)
    assert any(i.rule == "functional_interface_missing_peer" for i in issues)

    ir.connect("MOTOR_PWM", ("U1", "1"))
    assert not any(
        i.rule == "functional_interface_missing_peer"
        for i in check_functional_pin_completeness(ir, symbols)
    )


def test_validate_plan_reconciles_dropped_spec_signals():
    plan = [{
        "id": "MCU",
        "description": "controller",
        "roles": ["mcu"],
        "count": 1,
        "interface_nets": [],
    }, {
        "id": "DRIVER",
        "description": "motor",
        "roles": ["motor_driver"],
        "count": 1,
        "interface_nets": [],
    }]
    spec = {
        "parts_needed": [
            {"role": "mcu", "search_query": "MCU", "functional_kind": "microcontroller"},
            {
                "role": "motor_driver",
                "search_query": "driver",
                "functional_kind": "motor_driver",
                "quantity": 1,
            },
        ],
        "signals": [{
            "name": "MOTOR_PWM",
            "purpose": "speed",
            "peer": "controller",
            "protocol": "generic_control",
            "required": True,
            "owner_role": "motor_driver",
        }],
    }
    fixed, notes = validate_plan(plan, spec)
    assert any("MOTOR_PWM" in n for n in notes)
    driver = next(b for b in fixed if b["id"] == "DRIVER")
    assert any(n["name"] == "MOTOR_PWM" for n in driver["interface_nets"])
    pwm = next(n for n in driver["interface_nets"] if n["name"] == "MOTOR_PWM")
    assert pwm["peer"] == "controller"
    assert pwm["protocol"] == "generic_control"


def test_critical_controller_nets_route_before_ordinary_nets():
    ir = CircuitIR("prio")
    ir.add(Component("U1", "X:A", "a"))
    ir.add(Component("U2", "X:B", "b"))
    ir.connect("DECOUPLE", ("U1", "1"), ("U2", "1"))
    ir.connect("MOTOR_PWM", ("U1", "2"), ("U2", "2"))
    ir.connect("SDA", ("U1", "3"), ("U2", "3"), ("U2", "4"))
    ir.interface_contracts = [
        InterfaceContract("MOTOR_PWM", peer="controller", protocol="generic_control"),
        InterfaceContract("SDA", peer="controller", protocol="i2c"),
        InterfaceContract("HDR", peer="external", protocol="other"),
    ]
    ordered = sorted(ir.nets, key=lambda n: routing_net_priority(ir, n.name))
    assert [n.name for n in ordered] == ["MOTOR_PWM", "SDA", "DECOUPLE"]


def test_detected_i2c_bus_outranks_ordinary_net_without_contracts():
    """Legacy IR has no InterfaceContract; pin-membership I2C still routes first."""
    from circuitgen.pins import PinType

    sensor = SymbolDef("Sensor:TMP", "", [
        PinDef("1", "SCL", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("6", "SDA", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("4", "V+", PinType.PWRIN, 0, 0, 0, 2.54),
    ], reference_prefix="U")
    other = SymbolDef("Device:R", "", [
        PinDef("1", "", PinType.PASSIVE, 0, 0, 0, 2.54),
        PinDef("2", "", PinType.PASSIVE, 0, 0, 0, 2.54),
    ], reference_prefix="R")
    symbols = {sensor.lib_id: sensor, other.lib_id: other}
    ir = CircuitIR("legacy-i2c")
    ir.add(Component("U1", sensor.lib_id, "tmp"))
    ir.add(Component("R1", other.lib_id, "10k"))
    ir.connect("SCL", ("U1", "1"), ("R1", "1"))
    ir.connect("BIAS", ("R1", "2"), ("U1", "4"))
    assert routing_net_priority(ir, "SCL", symbols)[0] == 0
    assert routing_net_priority(ir, "BIAS", symbols)[0] == 2


def test_spi_clock_ranks_before_mosi_within_bus_tier():
    from circuitgen.pins import PinType

    flash = SymbolDef("Memory_Flash:W25Q32JV", "", [
        PinDef("1", "~{CS}", PinType.INPUT, 0, 0, 0, 2.54),
        PinDef("2", "DO", PinType.OUTPUT, 0, 0, 0, 2.54),
        PinDef("5", "DI", PinType.INPUT, 0, 0, 0, 2.54),
        PinDef("6", "CLK", PinType.INPUT, 0, 0, 0, 2.54),
    ], reference_prefix="U")
    mcu = SymbolDef("Controller:Small", "", [
        PinDef("1", "SCK", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("2", "MOSI", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("3", "MISO", PinType.BIDIR, 0, 0, 0, 2.54),
        PinDef("4", "NSS", PinType.BIDIR, 0, 0, 0, 2.54),
    ], reference_prefix="U")
    symbols = {flash.lib_id: flash, mcu.lib_id: mcu}
    ir = CircuitIR("spi-rank")
    ir.add(Component("U1", mcu.lib_id, "mcu"))
    ir.add(Component("U2", flash.lib_id, "flash"))
    ir.connect("SCLK", ("U1", "1"), ("U2", "6"))
    ir.connect("MOSI", ("U1", "2"), ("U2", "5"))
    ir.connect("MISO", ("U1", "3"), ("U2", "2"))
    ir.connect("CS", ("U1", "4"), ("U2", "1"))
    keys = {n: routing_net_priority(ir, n, symbols) for n in ("SCLK", "MOSI", "MISO", "CS")}
    assert keys["SCLK"] < keys["MOSI"] < keys["MISO"]
    assert keys["MISO"] < keys["CS"]
    assert keys["SCLK"][0] == keys["CS"][0] == 0
