from circuitgen.router import route_multi_terminal


def test_route_metrics_exposes_critical_stub_net_names():
    from circuitgen.emit import EmitPlan, RouteFailure, route_metrics
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
    plan = EmitPlan(
        wires=[((0.0, 0.0), (10.0, 0.0), "net.STATUS_LED")],
        net_routes={"MOTOR_PWM": "stubs", "STATUS_LED": "direct"},
        route_failures={
            "MOTOR_PWM": RouteFailure(
                "tree", "occupied_by_net", ("STATUS_LED",)
            )
        },
    )

    metrics = route_metrics(ir, symbols, plan)
    assert metrics["stub_net_names"] == ["MOTOR_PWM"]
    assert metrics["critical_stub_nets"] == ["MOTOR_PWM"]
    assert metrics["critical_wired_ratio"] == 0.0
    assert metrics["critical_by_protocol"]["generic_control"] == {
        "nets": 1, "stub_nets": 1,
    }
    assert metrics["route_failure_reasons"] == {"occupied_by_net": 1}
    assert metrics["route_geometry"] == {
        "STATUS_LED": {
            "wire_length_mm": 10.0,
            "bounding_span_mm": 10.0,
            "excess_length_mm": 0.0,
            "length_to_span_ratio": 1.0,
        }
    }
    assert metrics["critical_route_failures"]["MOTOR_PWM"] == {
        "stage": "tree",
        "reason": "occupied_by_net",
        "blocker_nets": ["STATUS_LED"],
    }


def test_emit_plan_records_terminal_limit_instead_of_bare_stub_fallback():
    from circuitgen.emit import RouteFailure, build_emit_plan
    from circuitgen.geometry import Placement
    from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
    from circuitgen.pins import PinType

    ir = CircuitIR("terminal_limit")
    symbol = SymbolDef("Test:Pin", "", [
        PinDef("1", "IO", PinType.BIDIR, 0, 0, 180, 2.54),
    ])
    placements = {}
    nodes = []
    for index in range(9):
        ref = f"U{index + 1}"
        ir.add(Component(ref, symbol.lib_id, "pin"))
        placements[ref] = {1: Placement(20.32 + index * 10.16, 20.32)}
        nodes.append((ref, "1"))
    ir.connect("BUS", *nodes)

    plan = build_emit_plan(ir, {symbol.lib_id: symbol}, placements)

    assert plan.net_routes["BUS"] == "stubs"
    assert plan.route_failures["BUS"] == RouteFailure(
        "tree", "terminal_limit"
    )


def test_tree_route_reports_the_net_occupying_a_terminal_escape():
    from circuitgen.emit import RoutingContext, _try_tree_wire
    from circuitgen.geometry import Placement
    from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
    from circuitgen.pins import PinType

    ir = CircuitIR("occupied_escape")
    symbol = SymbolDef("Test:Pin", "", [
        PinDef("1", "IO", PinType.BIDIR, 0, 0, 180, 2.54),
    ])
    for ref in ("U1", "U2"):
        ir.add(Component(ref, symbol.lib_id, "pin"))
    net = ir.connect("TARGET", ("U1", "1"), ("U2", "1"))
    symbols = {symbol.lib_id: symbol}
    placements = {
        "U1": {1: Placement(0, 0)},
        "U2": {1: Placement(20.32, 0)},
    }

    attempt = _try_tree_wire(
        ir,
        symbols,
        placements,
        net,
        RoutingContext(
            pin_points={},
            symbol_boxes={},
            stub_corridors={},
            routed_cells={(1.27, 0): "EARLY_NET"},
        ),
    )

    assert attempt.route is None
    assert attempt.failure is not None
    assert attempt.failure.reason == "occupied_by_net"
    assert attempt.failure.blocker_nets == ("EARLY_NET",)


def test_later_direct_candidate_cannot_cross_an_existing_net():
    from circuitgen.emit import build_emit_plan
    from circuitgen.geometry import Placement
    from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
    from circuitgen.pins import PinType
    from circuitgen.visual import check_routing

    ir = CircuitIR("crossing")
    symbol = SymbolDef("Test:Pin", "", [
        # Keep the pin outside the synthetic body box, like a real symbol.
        PinDef("1", "IO", PinType.BIDIR, 5.08, 0, 180, 2.54),
    ])
    for ref in ("H1", "H2", "V1", "V2"):
        ir.add(Component(ref, symbol.lib_id, "pin"))
    ir.connect("HORIZONTAL", ("H1", "1"), ("H2", "1"))
    ir.connect("VERTICAL", ("V1", "1"), ("V2", "1"))
    placements = {
        # Horizontal pin positions: (0, 0) and (20.32, 0).
        "H1": {1: Placement(-5.08, 0)},
        "H2": {1: Placement(25.4, 0, 180)},
        # Vertical pin positions: (10.16, -10.16) and (10.16, 10.16).
        "V1": {1: Placement(10.16, -15.24, 270)},
        "V2": {1: Placement(10.16, 15.24, 90)},
    }
    symbols = {symbol.lib_id: symbol}

    plan = build_emit_plan(ir, symbols, placements)

    assert plan.net_routes["HORIZONTAL"] == "direct"
    assert plan.net_routes["VERTICAL"] == "stubs"
    assert plan.route_failures["VERTICAL"].reason == "occupied_by_net"
    assert plan.route_failures["VERTICAL"].blocker_nets == ("HORIZONTAL",)
    assert not [
        issue for issue in check_routing(ir, symbols, placements, plan)
        if issue.rule == "wire_crosses_foreign_wire"
    ]


def test_l_route_uses_the_same_existing_wire_occupancy():
    from circuitgen.emit import RoutingContext, _try_l_wire
    from circuitgen.geometry import Placement
    from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
    from circuitgen.pins import PinType

    ir = CircuitIR("l_occupancy")
    symbol = SymbolDef("Test:Pin", "", [
        PinDef("1", "IO", PinType.BIDIR, 5.08, 0, 180, 2.54),
    ])
    for ref in ("U1", "U2"):
        ir.add(Component(ref, symbol.lib_id, "pin"))
    net = ir.connect("L_NET", ("U1", "1"), ("U2", "1"))
    placements = {
        # Pins at (0, 0), outward right, and (10.16, 10.16), outward up.
        # Only the (10.16, 0) L corner satisfies both pin directions.
        "U1": {1: Placement(-5.08, 0)},
        "U2": {1: Placement(10.16, 15.24, 90)},
    }
    context = RoutingContext(
        pin_points={},
        symbol_boxes={},
        stub_corridors={},
        routed_cells={(5.08, 0): "TREE_NET"},
    )

    route = _try_l_wire(
        ir, {symbol.lib_id: symbol}, placements, net, context
    )

    assert route is None
    blockage = context.validate_segments(
        [((0, 0), (10.16, 0)), ((10.16, 0), (10.16, 10.16))],
        own_refs={"U1", "U2"},
        own_pins={("U1", "1"), ("U2", "1")},
        net_name="L_NET",
    )
    assert blockage is not None
    assert blockage.reason == "occupied_by_net"
    assert blockage.blocker_nets == ("TREE_NET",)


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
