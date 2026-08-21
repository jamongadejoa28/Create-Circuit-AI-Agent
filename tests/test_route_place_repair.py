"""Local placement repairs for critical route failures."""

from __future__ import annotations

from circuitgen.emit import EmitPlan, RouteFailure, build_emit_plan
from circuitgen.geometry import GRID, Placement, pin_absolute_position
from circuitgen.ir import CircuitIR, Component, InterfaceContract, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.route_debug_overlay import write_route_debug_overlay
from circuitgen.route_place_repair import (
    critical_failures_for_repair,
    repair_placements_for_route_failures,
    snap_pin_to_grid,
)


def _sym(lib_id: str, pins: list[tuple], prefix: str = "U") -> SymbolDef:
    return SymbolDef(
        lib_id,
        "",
        [PinDef(str(n), name, etype, x, y, orient, 2.54) for n, name, etype, x, y, orient in pins],
        reference_prefix=prefix,
    )


def test_snap_pin_to_grid_moves_off_grid_anchor():
    pin = PinDef("1", "IO", PinType.BIDIR, 5.08, 0, 180, 2.54)
    # Anchor chosen so pin tip is off the 1.27 grid.
    place = Placement(10.0, 20.0)
    assert not all(
        abs(v / GRID - round(v / GRID)) <= 0.005
        for v in pin_absolute_position(place, pin)
    )
    snapped = snap_pin_to_grid(place, pin)
    assert snapped is not None
    assert all(
        abs(v / GRID - round(v / GRID)) <= 0.005
        for v in pin_absolute_position(snapped, pin)
    )


def test_off_grid_critical_net_is_snapped_then_routable():
    driver = _sym("Driver:Small", [
        (1, "PWM", PinType.INPUT, 5.08, 0, 180),
    ], prefix="U")
    ctrl = _sym("Controller:Small", [
        (1, "GPIO", PinType.BIDIR, 5.08, 0, 0),
    ], prefix="U")
    # Keep pin tips outside bodies; controller on-grid, driver deliberately off.
    symbols = {driver.lib_id: driver, ctrl.lib_id: ctrl}
    ir = CircuitIR("snap-pwm")
    ir.add(Component("U1", ctrl.lib_id, "mcu"))
    ir.add(Component("U2", driver.lib_id, "drv"))
    ir.controller_refs = ["U1"]
    ir.controller_required = True
    ir.interface_contracts.append(InterfaceContract(
        "MOTOR_PWM", peer="controller", protocol="generic_control",
    ))
    ir.connect("MOTOR_PWM", ("U1", "1"), ("U2", "1"))
    placements = {
        "U1": {1: Placement(0.0, 50.8)},
        "U2": {1: Placement(40.0, 50.8, 180)},  # off-grid pin tip
    }

    plan = build_emit_plan(ir, symbols, placements)
    assert plan.net_routes["MOTOR_PWM"] == "stubs"
    assert plan.route_failures["MOTOR_PWM"].reason == "off_grid_terminal"

    failures = critical_failures_for_repair(ir, symbols, plan)
    assert failures and failures[0][0] == "MOTOR_PWM"
    repaired, notes = repair_placements_for_route_failures(
        ir, symbols, placements, failures
    )
    assert notes
    plan2 = build_emit_plan(ir, symbols, repaired)
    assert plan2.net_routes["MOTOR_PWM"] in ("direct", "l", "tree")
    assert "MOTOR_PWM" not in plan2.route_failures


def test_route_debug_overlay_writes_svg(tmp_path):
    symbols = {
        "Test:Pin": _sym("Test:Pin", [
            (1, "IO", PinType.BIDIR, 5.08, 0, 180),
        ]),
    }
    ir = CircuitIR("overlay")
    ir.add(Component("U1", "Test:Pin", "a"))
    ir.add(Component("U2", "Test:Pin", "b"))
    ir.connect("SIG", ("U1", "1"), ("U2", "1"))
    placements = {
        "U1": {1: Placement(0.0, 0.0)},
        "U2": {1: Placement(25.4, 0.0, 180)},
    }
    plan = build_emit_plan(ir, symbols, placements)
    path = write_route_debug_overlay(
        ir, symbols, placements, plan, tmp_path / "overlay.route-debug.svg"
    )
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<?xml")
    assert "SIG" in text or "routes:" in text
