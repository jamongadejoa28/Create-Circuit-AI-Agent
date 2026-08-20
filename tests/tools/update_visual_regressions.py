#!/usr/bin/env python3
"""Regenerate the small, tracked schematic snapshots used for visual review."""

from __future__ import annotations

import json
from pathlib import Path

from circuitgen.emit import (
    build_emit_plan,
    emit_schematic,
    normalize_placements,
    route_metrics,
)
from circuitgen.ir import CircuitIR, Component, InterfaceContract
from circuitgen.kicad_cli import export_svg
from circuitgen.netnames import is_ground_pin
from circuitgen.pinfunctions import resolve_function_ending
from circuitgen.pins import PinType
from circuitgen.place import heuristic_place
from circuitgen.project import write_project
from circuitgen.symbols import load_symbols


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "tests" / "artifacts" / "visual_regressions"
FIXTURES = ROOT / "tests" / "fixtures" / "visual_regressions"


def i2c_terminal_limit_board() -> tuple[CircuitIR, dict]:
    """Nine-terminal SDA/SCL buses that document the current router limit."""
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    sensor = "Sensor_Temperature:Si7050-A20"
    lib_ids = [mcu, sensor, "Device:R", "power:+3V3", "power:GND"]
    symbols = load_symbols(lib_ids)
    ir = CircuitIR("i2c_terminal_limit")
    ir.add(Component("U1", mcu, "STM32G474RET6", group="MCU"))
    for index in range(1, 8):
        ir.add(Component(
            f"U{index + 1}", sensor, "Si7050", group=f"SENSOR{index}"
        ))
    ir.add(Component("R1", "Device:R", "10k", group="I2C"))
    ir.add(Component("R2", "Device:R", "10k", group="I2C"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    mcu_sym = symbols[mcu]
    sda = resolve_function_ending(mcu, mcu_sym, "SDA")
    scl = resolve_function_ending(mcu, mcu_sym, "SCL")
    assert sda and scl
    sensor_refs = [f"U{index + 1}" for index in range(1, 8)]
    ir.connect("SDA", ("U1", sda[0]), *[(ref, "1") for ref in sensor_refs], ("R1", "2"))
    ir.connect("SCL", ("U1", scl[0]), *[(ref, "6") for ref in sensor_refs], ("R2", "2"))
    ir.connect(
        "+3V3", ("#PWR01", "1"), ("R1", "1"), ("R2", "1"),
        *[(ref, "5") for ref in sensor_refs],
    )
    ir.connect("GND", ("#PWR02", "1"), *[(ref, "2") for ref in sensor_refs])

    used = {(ref, str(pin)) for net in ir.nets for ref, pin in net.nodes}
    for pin in mcu_sym.pins:
        node = ("U1", str(pin.number))
        if node in used or pin.hidden:
            continue
        if pin.etype == PinType.PWRIN:
            ir.connect("GND" if is_ground_pin(pin.name or "") else "+3V3", node)
        else:
            ir.nc_pins.append(node)
    for ref in sensor_refs:
        ir.nc_pins.extend([(ref, "3"), (ref, "4")])

    ir.controller_required = True
    ir.controller_refs = ["U1"]
    for index in range(1, 8):
        for net in ("SDA", "SCL"):
            ir.interface_contracts.append(InterfaceContract(
                net, owner_group=f"SENSOR{index}", peer="controller",
                protocol="i2c", purpose=f"sensor {index} bus",
            ))
    return ir, symbols


def main() -> None:
    ir, symbols = i2c_terminal_limit_board()
    placements = heuristic_place(ir, symbols)
    canonical = normalize_placements(ir, symbols, placements)
    plan = build_emit_plan(ir, symbols, canonical)
    metrics = route_metrics(ir, symbols, plan)

    GENERATED.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    schematic = GENERATED / f"{ir.name}.kicad_sch"
    schematic.write_text(
        emit_schematic(ir, symbols, canonical, plan), encoding="utf-8"
    )
    write_project(schematic)
    svg_dir = GENERATED / "svg"
    rendered = export_svg(schematic, svg_dir)
    if rendered.returncode != 0:
        raise SystemExit(rendered.stderr or "KiCad SVG export failed")
    # KiCad on Windows emits CRLF. Normalize the tracked text fixture so Git
    # whitespace checks stay useful and reviews do not show carriage returns.
    source_svg = svg_dir / f"{ir.name}.svg"
    target_svg = FIXTURES / f"{ir.name}.svg"
    raw_svg = source_svg.read_bytes().replace(b"\r\n", b"\n")
    target_svg.write_bytes(b"\n".join(
        line.rstrip(b" \t") for line in raw_svg.split(b"\n")
    ))
    (FIXTURES / f"{ir.name}.metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
