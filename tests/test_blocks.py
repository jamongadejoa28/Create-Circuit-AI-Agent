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
    assert "c" not in fixed[0]["roles"]      # orphan dropped, not stuffed
    assert any("dropped" in n and "'c'" in n for n in notes)
    assert "ghost" not in fixed[1]["roles"]  # unknown role dropped
    assert notes


def test_missing_block_ir_skipped():
    ir, notes = instantiate_blocks("t", PLAN, {}, rails=["GND"])
    assert not ir.components
    assert any("skipped" in n for n in notes)
