from circuitgen.router import route_multi_terminal


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
