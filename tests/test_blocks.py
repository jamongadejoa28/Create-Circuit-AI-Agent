"""Block decomposition: deterministic instantiation/merge and the mock
agent flow through plan → per-block synthesis → merge → pipeline."""

from pathlib import Path

import pytest

from circuitgen.blocks import instantiate_blocks, validate_plan
from circuitgen.ir import CircuitIR, Component
from circuitgen.ir_json import ir_from_json


def _led_block_ir():
    return ir_from_json({
        "name": "b",
        "components": [
            {"ref": "R1", "lib_id": "Device:R", "value": "330R", "footprint": "Resistor_SMD:R_0805_2012Metric"},
            {"ref": "D1", "lib_id": "Device:LED", "value": "LED", "footprint": "LED_SMD:LED_0805_2012Metric"},
        ],
        "nets": [
            {"name": "CTRL{n}", "nodes": [{"ref": "R1", "pin": "1"}]},   # interface
            {"name": "MID", "nodes": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "2"}]},  # local
            {"name": "GND", "nodes": [{"ref": "D1", "pin": "1"}]},        # rail
        ],
        "nc_pins": [],
    })


PLAN = [
    {"id": "LEDBLK", "description": "indicator", "roles": ["led"], "count": 3,
     "interface_nets": [{"name": "CTRL{n}", "purpose": "drive input"}]},
]


def test_instantiate_repeated_block():
    ir, notes = instantiate_blocks("t", PLAN, {"LEDBLK": _led_block_ir()}, rails=["+5V", "GND"])
    # 3 instances × 2 components, globally renumbered
    assert sorted(ir.components) == ["D1", "D2", "D3", "R1", "R2", "R3"]
    names = {n.name for n in ir.nets}
    # interface nets instance-stamped, locals namespaced, rail shared once
    assert {"CTRL1", "CTRL2", "CTRL3"} <= names
    assert {"LEDBLK1_MID", "LEDBLK2_MID", "LEDBLK3_MID"} <= names
    gnd = [n for n in ir.nets if n.name == "GND"]
    assert len(gnd) == 1 and len(gnd[0].nodes) == 3  # all cathodes on one rail net
    assert {c.group for c in ir.components.values()} == {"LEDBLK1", "LEDBLK2", "LEDBLK3"}


def test_single_instance_block_namespacing():
    plan = [{"id": "PWR", "description": "supply", "roles": [], "count": 1,
             "interface_nets": [{"name": "VOUT", "purpose": "regulated"}]}]
    blk = ir_from_json({
        "name": "b",
        "components": [{"ref": "C1", "lib_id": "Device:C", "value": "10uF"}],
        "nets": [
            {"name": "VOUT", "nodes": [{"ref": "C1", "pin": "1"}]},
            {"name": "FB", "nodes": [{"ref": "C1", "pin": "2"}]},
        ],
    })
    ir, _ = instantiate_blocks("t", plan, {"PWR": blk}, rails=["GND"])
    names = {n.name for n in ir.nets}
    assert "VOUT" in names          # interface keeps global name
    assert "PWR_FB" in names        # local gets block prefix


def test_validate_plan_orphans_and_dup_ids():
    spec = {"parts_needed": [{"role": "a"}, {"role": "b"}, {"role": "c"}]}
    plan = [
        {"id": "X", "roles": ["a"], "count": 1, "interface_nets": []},
        {"id": "X", "roles": ["b", "ghost"], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert fixed[1]["id"] == "XX"
    assert any(b["roles"] == ["c"] for b in fixed)  # orphan restored independently
    assert any("restored omitted role c" in n for n in notes)
    assert "ghost" not in fixed[1]["roles"]  # unknown role dropped
    assert notes


def test_validate_plan_corrects_repeated_hardware_count_from_requirements():
    spec = {
        "parts_needed": [
            {"role": "encoder", "search_query": "AS5048A", "quantity": 4},
            {"role": "driver", "search_query": "BLDC driver", "quantity": 4},
        ]
    }
    plan = [
        {"id": "ENC", "roles": ["encoder"], "count": 1, "interface_nets": []},
        {"id": "DRV", "roles": ["driver"], "count": 4, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert fixed[0]["count"] == 4
    assert fixed[1]["count"] == 4
    assert any("ENC" in n and "corrected" in n for n in notes)


def test_validate_plan_namespaces_repeated_control_but_keeps_shared_spi_bus():
    spec = {"parts_needed": [{"role": "encoder", "quantity": 4}]}
    plan = [{
        "id": "ENC", "roles": ["encoder"], "count": 4,
        "interface_nets": [
            {"name": "SPI_SCK", "purpose": "shared clock"},
            {"name": "SPI_MISO", "purpose": "shared data"},
            {"name": "ENC_CS", "purpose": "individual select"},
        ],
    }]
    fixed, notes = validate_plan(plan, spec)
    assert [n["name"] for n in fixed[0]["interface_nets"]] == [
        "SPI_SCK", "SPI_MISO", "ENC_CS{n}"
    ]
    assert any("ENC_CS" in n and "per-instance" in n for n in notes)


def test_validate_plan_removes_passive_only_decoupling_block():
    spec = {"parts_needed": [
        {"role": "driver", "quantity": 4},
        {"role": "Decoupling Capacitor", "quantity": 4},
    ]}
    plan = [
        {"id": "MOTOR", "roles": ["driver"], "count": 4, "interface_nets": []},
        {"id": "DECOUPLING", "roles": ["Decoupling Capacitor"], "count": 4, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert [b["id"] for b in fixed] == ["MOTOR"]
    assert any("passive-only" in n for n in notes)


def test_validate_plan_assigns_repeated_peripheral_role_to_one_owner():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "encoder", "search_query": "SPI encoder", "quantity": 4},
        {"role": "can_interface", "search_query": "CAN transceiver", "quantity": 1},
    ]}
    plan = [
        {"id": "MCU", "description": "main controller with encoders and CAN", "roles": ["controller", "encoder", "can_interface"], "count": 1, "interface_nets": []},
        {"id": "ENCODER", "description": "SPI encoder interface", "roles": ["encoder"], "count": 4, "interface_nets": []},
        {"id": "CAN", "description": "CAN communication", "roles": ["can_interface"], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    by_id = {b["id"]: b for b in fixed}
    assert by_id["MCU"]["roles"] == ["controller"]
    assert by_id["MCU"]["count"] == 1
    assert by_id["ENCODER"]["roles"] == ["encoder"]
    assert by_id["CAN"]["roles"] == ["can_interface"]
    assert sum("encoder" in b["roles"] for b in fixed) == 1
    assert any("duplicate ownership" in note for note in notes)


def test_validate_plan_splits_repeated_encoder_out_of_singleton_mcu_block():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "encoder", "search_query": "SPI encoder", "quantity": 4},
        {"role": "can_interface", "search_query": "CAN transceiver", "quantity": 1},
    ]}
    plan = [{
        "id": "MCU", "description": "controller with encoders and CAN",
        "roles": ["controller", "encoder", "can_interface"], "count": 4,
        "interface_nets": [
            {"name": "SPI_SCK", "purpose": "SPI encoder clock"},
            {"name": "SPI_CS{n}", "purpose": "encoder select"},
            {"name": "CAN_TX", "purpose": "CAN transmit"},
        ],
    }]
    fixed, notes = validate_plan(plan, spec)
    mcu = next(b for b in fixed if b["id"] == "MCU")
    enc = next(b for b in fixed if b["roles"] == ["encoder"])
    assert mcu["roles"] == ["controller", "can_interface"] and mcu["count"] == 1
    assert enc["count"] == 4
    assert {n["name"] for n in enc["interface_nets"]} == {"SPI_SCK", "SPI_CS{n}"}
    assert any("split from mixed block" in note for note in notes)


def test_validate_plan_removes_redundant_empty_motor_blocks():
    spec = {"parts_needed": [{"role": "driver", "search_query": "BLDC motor driver", "quantity": 4}]}
    plan = [
        {"id": "MOTOR1", "roles": ["driver"], "count": 4, "interface_nets": []},
        {"id": "MOTOR2", "roles": [], "count": 1, "interface_nets": []},
        {"id": "MOTOR3", "roles": [], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert [b["id"] for b in fixed] == ["MOTOR1"]
    assert any("empty-role" in note for note in notes)


def test_validate_plan_restores_power_requirements_and_reset_without_repeating_mcu():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "reset_button", "search_query": "push button", "quantity": 1},
        {"role": "power_supply", "search_query": "power supply", "quantity": 1},
        {"role": "bulk_capacitor", "search_query": "bulk capacitor", "quantity": 1},
        {"role": "fuse", "search_query": "fuse", "quantity": 1},
        {"role": "tvss_diode", "search_query": "TVS diode", "quantity": 1},
    ]}
    plan = [{
        "id": "MCU", "description": "controller", "roles": ["controller"],
        "count": 1, "interface_nets": [],
    }]
    fixed, notes = validate_plan(plan, spec)
    mcu = next(b for b in fixed if b["id"] == "MCU")
    power = next(b for b in fixed if b["id"] == "POWER_REQUIREMENTS")
    assert mcu["roles"] == ["controller", "reset_button"]
    assert mcu["count"] == 1
    assert set(power["roles"]) == {
        "power_supply", "bulk_capacitor", "fuse", "tvss_diode"
    }
    assert power["count"] == 1
    assert any("restored omitted power roles" in note for note in notes)


def test_missing_block_ir_skipped():
    ir, notes = instantiate_blocks("t", PLAN, {}, rails=["GND"])
    assert not ir.components
    assert any("skipped" in n for n in notes)


def test_a_block_with_no_interface_net_is_an_island():
    """Measured on a real request (STM32G474 + 4 BLDC + 4 AS5048A + CAN + UART
    + battery monitor): MCU and COMM declared CAN_H/CAN_L/TX/RX and those four
    were exactly the signals that connected. MOTOR, ENCODER and BATTERY
    declared `[]` and the board came out with 100 one-pin nets out of 113.
    """
    from circuitgen.blocks import islands

    plan = [
        {"id": "MCU", "count": 1, "roles": ["controller"],
         "interface_nets": [{"name": "CAN_H", "purpose": "CAN high"}]},
        {"id": "MOTOR", "count": 4, "roles": ["motor_driver"], "interface_nets": []},
        {"id": "ENCODER", "count": 4, "roles": ["encoder"], "interface_nets": []},
    ]
    assert islands(plan) == ["MOTOR", "ENCODER"]

    # a single-block plan has nothing to be an island from
    assert islands([{"id": "ONLY", "count": 1, "interface_nets": []}]) == []


def test_a_repeated_block_gets_one_net_per_instance():
    """Measured on the 4-motor board: the plan correctly declared
    MOTOR{n}_PWM_A for count=4, the template IR came back naming it
    MOTOR1_PWM_A, and every instance then joined that same net — all four
    drivers on one PWM line, all four encoders on one chip select, and no
    MOTOR2/3/4 or ENC2/3/4 net anywhere on the board."""
    from circuitgen.blocks import instantiate_blocks
    from circuitgen.ir import CircuitIR, Component

    plan = [{
        "id": "MOTOR", "count": 4, "roles": ["motor_driver"],
        "interface_nets": [
            {"name": "MOTOR{n}_PWM_A", "purpose": "phase A"},
            {"name": "SPI_SCK", "purpose": "shared bus"},
        ],
    }]
    # the template as synthesis returns it: the per-instance net rendered
    # with a literal 1, the shared bus plain, plus a block-local net
    tmpl = CircuitIR("motor")
    tmpl.add(Component("U1", "X:DRV", "DRV"))
    tmpl.add(Component("C1", "Device:C", "100nF"))
    tmpl.connect("MOTOR1_PWM_A", ("U1", "15"))
    tmpl.connect("SPI_SCK", ("U1", "2"))
    tmpl.connect("VM_LOCAL", ("U1", "8"), ("C1", "1"))

    merged, _notes = instantiate_blocks("board", plan, {"MOTOR": tmpl}, ["GND"])
    names = {n.name: len(n.nodes) for n in merged.nets}

    # one PWM net per instance, one pin each
    assert {f"MOTOR{i}_PWM_A" for i in range(1, 5)} <= set(names)
    assert [names[f"MOTOR{i}_PWM_A"] for i in range(1, 5)] == [1, 1, 1, 1]
    # the shared bus stays shared: all four drivers on it
    assert names["SPI_SCK"] == 4
    # a block-local net is scoped per instance, not merged
    assert {f"MOTOR{i}_VM_LOCAL" for i in range(1, 5)} <= set(names)


def test_an_interface_net_that_misses_the_hub_is_still_dangling():
    """The MCU-wiring pass only offered a pin to nets holding exactly ONE pin,
    so a signal shared by four peripherals — reaching no controller pin at all
    — was never a candidate. Measured on the 4-motor board: nine MOTORn_*/
    ENCn_* nets, four pins each, zero MCU pins, and this pass skipped them.

    Runs the real pass with a model that refuses, so the deterministic
    round-robin fallback does the assignment.
    """
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()

    ir = CircuitIR("board")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "MCU"))
    for n in range(2, 6):
        ir.add(Component(f"U{n}", "Driver_Motor:DRV8311H", "DRV"))
    ir.connect("MOTOR_PWM", *[(f"U{n}", "15") for n in range(2, 6)])
    ir.connect("ALREADY_OK", ("U1", "20"), ("U2", "14"))

    notes = agent.wire_mcu_interfaces(
        ir, [{"net": "MOTOR_PWM"}, {"net": "ALREADY_OK"}]
    )
    assert any("MOTOR_PWM" in n for n in notes), notes
    pwm = next(n for n in ir.nets if n.name == "MOTOR_PWM")
    assert "U1" in {r for r, _ in pwm.nodes}, pwm.nodes
    # a net the hub already reaches is left alone
    assert not any("ALREADY_OK" in n for n in notes), notes
