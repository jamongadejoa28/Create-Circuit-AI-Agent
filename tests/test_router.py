from circuitgen.router import route_multi_terminal


def test_route_metrics_exposes_critical_stub_net_names():
    from circuitgen.emit import EmitPlan, route_metrics
    from circuitgen.ir import CircuitIR, Component, InterfaceContract, PinDef, SymbolDef
    from circuitgen.pins import PinType

    ir = CircuitIR("metrics")
    ir.add(Component("U1", "Test:Controller", "controller", group="MCU"))
    ir.add(Component("U2", "Test:Driver", "driver", group="DRIVER"))
    ir.add(Component("J1", "Test:Header", "header", group="DRIVER"))
    ir.connect("MOTOR_PWM", ("U1", "1"), ("U2", "1"))
    ir.connect("STATUS_LED", ("U1", "2"), ("J1", "1"))
    ir.interface_contracts.append(InterfaceContract(
        "MOTOR_PWM", owner_group="DRIVER", peer="controller",
        protocol="generic_control",
    ))
    symbols = {
        lib_id: SymbolDef(lib_id, "", [
            PinDef("1", "IO1", PinType.BIDIR, 0, 0, 0, 2.54),
            PinDef("2", "IO2", PinType.BIDIR, 0, 0, 0, 2.54),
        ])
        for lib_id in ("Test:Controller", "Test:Driver", "Test:Header")
    }
    plan = EmitPlan(net_routes={"MOTOR_PWM": "stubs", "STATUS_LED": "direct"})

    metrics = route_metrics(ir, symbols, plan)
    assert metrics["stub_net_names"] == ["MOTOR_PWM"]
    assert metrics["critical_stub_nets"] == ["MOTOR_PWM"]
    assert metrics["critical_wired_ratio"] == 0.0
    assert metrics["critical_by_protocol"]["generic_control"] == {
        "nets": 1, "stub_nets": 1,
    }


def test_terminal_limit_visual_regression_is_tracked_with_metrics():
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "visual_regressions"
    svg = fixture / "i2c_terminal_limit.svg"
    metrics = json.loads(
        (fixture / "i2c_terminal_limit.metrics.json").read_text(encoding="utf-8")
    )
    assert svg.is_file() and "<svg" in svg.read_text(encoding="utf-8")[:500]
    assert metrics["critical_stub_nets"] == ["SCL", "SDA"]


def _on_segment(p, a, b):
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def test_multi_terminal_router_connects_three_pins_with_branch():
    route = route_multi_terminal([(0, 0), (20.32, 0), (10.16, 15.24)], [], grid=2.54)
    assert route is not None
    assert len(route.paths) == 2
    assert all(a[0] == b[0] or a[1] == b[1] for a, b in route.segments)


def test_router_avoids_symbol_body_obstacle():
    obstacle = (7.0, -3.0, 13.0, 3.0)
    route = route_multi_terminal([(0, 0), (20.32, 0)], [obstacle], grid=2.54, clearance=1.0)
    assert route is not None
    for a, b in route.segments:
        assert not _on_segment((10.16, 0), a, b)


def test_router_returns_none_when_terminal_is_sealed():
    walls = [(-3, -3, 3, -2), (-3, 2, 3, 3), (-3, -3, -2, 3), (2, -3, 3, 3)]
    assert route_multi_terminal([(0, 0), (20.32, 0)], walls, grid=1.0, margin_cells=5) is None
