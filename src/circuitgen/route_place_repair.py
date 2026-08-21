"""Local placement repairs driven by emit route-failure reasons.

Global re-placement is out of scope. Only critical (required controller
contract) stub nets get a small deterministic nudge, then emit is rebuilt.
"""

from __future__ import annotations

from copy import deepcopy

from .emit import EmitPlan, RouteFailure, route_metrics
from .geometry import GRID, Placement, pin_absolute_position, pin_outward_dir
from .ir import CircuitIR, PinDef, SymbolDef
from .place import _body_box

REPAIR_REASONS = frozenset({
    "off_grid_terminal",
    "escape_blocked",
    "foreign_geometry",
    "astar_no_path",
})
MAX_ROUTE_PLACE_REPAIRS = 2
_NUDGE = 2 * GRID
_SHEET_MIN, _SHEET_MAX_X, _SHEET_MAX_Y = 15.24, 390.0, 260.0


def critical_failures_for_repair(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    plan: EmitPlan,
) -> list[tuple[str, RouteFailure]]:
    """Critical stub nets whose failure reason has a local placement fix."""
    metrics = route_metrics(ir, symbols, plan)
    critical = set(metrics.get("critical_stub_nets") or [])
    out: list[tuple[str, RouteFailure]] = []
    for name in critical:
        failure = plan.route_failures.get(name)
        if failure is None or failure.reason not in REPAIR_REASONS:
            continue
        out.append((name, failure))
    return out


def _pin_on_grid(pos: tuple[float, float]) -> bool:
    return all(abs(v / GRID - round(v / GRID)) <= 0.005 for v in pos)


def _movable(
    ref: str,
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> bool:
    comp = ir.components.get(ref)
    if comp is None or ref not in placements:
        return False
    if ref in ir.controller_refs:
        return False
    sym = symbols.get(comp.lib_id)
    if sym is None or sym.is_power:
        return False
    if len(sym.pins) > 4 or len(placements[ref]) != 1:
        return False
    prefix = comp.lib_id.split(":", 1)[0]
    if prefix.startswith(("MCU_", "CPU_", "RF_Module", "Connector")):
        return False
    return True


def _unit_for(ref: str, pin_no: str, ir: CircuitIR, symbols, placements) -> int:
    sym = symbols[ir.components[ref].lib_id]
    pin = sym.pin(str(pin_no))
    units = placements[ref]
    if pin.unit in units:
        return pin.unit
    if pin.unit == 0 and len(units) == 1:
        return next(iter(units))
    return next(iter(units))


def _net_movable_endpoints(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
    net_name: str,
) -> list[tuple[str, int, PinDef]]:
    net = next((n for n in ir.nets if n.name == net_name), None)
    if net is None:
        return []
    out: list[tuple[str, int, PinDef]] = []
    for ref, pin_no in net.nodes:
        if not _movable(ref, ir, symbols, placements):
            continue
        unit = _unit_for(ref, pin_no, ir, symbols, placements)
        pin = symbols[ir.components[ref].lib_id].pin(str(pin_no))
        out.append((ref, unit, pin))
    return out


def _occupancy(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
) -> tuple[
    dict[str, tuple[float, float, float, float]],
    dict[str, list[tuple[float, float]]],
]:
    boxes: dict[str, tuple[float, float, float, float]] = {}
    pin_pts: dict[str, list[tuple[float, float]]] = {}
    for ref, units_map in placements.items():
        sym = symbols[ir.components[ref].lib_id]
        for unit, place in units_map.items():
            box = _body_box(sym, unit, place)
            prev = boxes.get(ref)
            boxes[ref] = (
                min(prev[0], box[0]), min(prev[1], box[1]),
                max(prev[2], box[2]), max(prev[3], box[3]),
            ) if prev else box
            pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
            pin_pts.setdefault(ref, []).extend(
                pin_absolute_position(place, p) for p in pins
            )
    return boxes, pin_pts


def _fits(
    ref: str,
    box: tuple[float, float, float, float],
    pts: list[tuple[float, float]],
    boxes: dict[str, tuple[float, float, float, float]],
    pin_pts: dict[str, list[tuple[float, float]]],
) -> bool:
    if box[0] < _SHEET_MIN or box[1] < _SHEET_MIN:
        return False
    if box[2] > _SHEET_MAX_X or box[3] > _SHEET_MAX_Y:
        return False
    for other, ob in boxes.items():
        if other == ref:
            continue
        if box[2] > ob[0] and box[0] < ob[2] and box[3] > ob[1] and box[1] < ob[3]:
            return False
    for other, opts in pin_pts.items():
        if other == ref:
            continue
        for px, py in opts:
            if box[0] - 0.01 <= px <= box[2] + 0.01 and box[1] - 0.01 <= py <= box[3] + 0.01:
                return False
        for qx, qy in pts:
            if any(abs(qx - px) < 0.02 and abs(qy - py) < 0.02 for px, py in opts):
                return False
    return True


def _apply_place(
    placements: dict[str, dict[int, Placement]],
    ref: str,
    unit: int,
    place: Placement,
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
) -> bool:
    boxes, pin_pts = _occupancy(ir, symbols, placements)
    sym = symbols[ir.components[ref].lib_id]
    trial = deepcopy(placements)
    trial[ref] = {**trial[ref], unit: place}
    box = _body_box(sym, unit, place)
    pins = [p for p in sym.pins if p.unit in (0, unit)] or sym.pins
    pts = [pin_absolute_position(place, p) for p in pins]
    # Drop this ref from occupancy so it does not collide with itself.
    boxes.pop(ref, None)
    pin_pts.pop(ref, None)
    if not _fits(ref, box, pts, boxes, pin_pts):
        return False
    placements[ref][unit] = place
    return True


def snap_pin_to_grid(place: Placement, pin: PinDef) -> Placement | None:
    """Shift (and optionally rotate) so the pin tip lands on the KiCad grid."""
    rotations = (place.rotation,) + tuple(
        rot for rot in (0, 90, 180, 270) if rot != place.rotation
    )
    for rot in rotations:
        candidate = Placement(place.x, place.y, rot, place.mirror)
        pos = pin_absolute_position(candidate, pin)
        snapped = Placement(
            round(candidate.x + (round(pos[0] / GRID) * GRID - pos[0]), 4),
            round(candidate.y + (round(pos[1] / GRID) * GRID - pos[1]), 4),
            rot,
            place.mirror,
        )
        if _pin_on_grid(pin_absolute_position(snapped, pin)):
            return snapped
    return None


def _nudge_candidates(place: Placement, pin: PinDef) -> list[Placement]:
    outward = pin_outward_dir(place, pin)
    dirs = [
        outward,
        (-outward[0], -outward[1]),
        (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
    ]
    seen: set[tuple[float, float, int]] = set()
    out: list[Placement] = []
    for dx, dy in dirs:
        for step in range(1, 5):
            cand = Placement(
                round(place.x + dx * _NUDGE * step, 4),
                round(place.y + dy * _NUDGE * step, 4),
                place.rotation,
                place.mirror,
            )
            key = (cand.x, cand.y, cand.rotation)
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
    return out


def _push_apart_candidates(
    place: Placement,
    from_pos: tuple[float, float],
    to_pos: tuple[float, float],
) -> list[Placement]:
    vx, vy = to_pos[0] - from_pos[0], to_pos[1] - from_pos[1]
    if abs(vx) + abs(vy) < 0.01:
        dirs = [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]
    elif abs(vx) >= abs(vy):
        dirs = [(1.0 if vx >= 0 else -1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]
    else:
        dirs = [(0.0, 1.0 if vy >= 0 else -1.0), (1.0, 0.0), (-1.0, 0.0)]
    out: list[Placement] = []
    for dx, dy in dirs:
        for step in (1, 2, 3):
            out.append(Placement(
                round(place.x + dx * _NUDGE * step, 4),
                round(place.y + dy * _NUDGE * step, 4),
                place.rotation,
                place.mirror,
            ))
    return out


def repair_placements_for_route_failures(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    placements: dict[str, dict[int, Placement]],
    failures: list[tuple[str, RouteFailure]],
) -> tuple[dict[str, dict[int, Placement]], list[str]]:
    """Return adjusted placements and notes. Unchanged copy if nothing moves."""
    adjusted = {
        ref: dict(units) for ref, units in placements.items()
    }
    notes: list[str] = []

    for net_name, failure in failures:
        endpoints = _net_movable_endpoints(ir, symbols, adjusted, net_name)
        if failure.reason == "off_grid_terminal":
            # Controllers/ICs may be off-grid too — try any node, prefer movable.
            net = next((n for n in ir.nets if n.name == net_name), None)
            targets: list[tuple[str, int, PinDef]] = list(endpoints)
            if net is not None:
                for ref, pin_no in net.nodes:
                    if any(t[0] == ref for t in targets):
                        continue
                    if ref not in adjusted or ref not in ir.components:
                        continue
                    sym = symbols.get(ir.components[ref].lib_id)
                    if sym is None or sym.is_power:
                        continue
                    unit = _unit_for(ref, pin_no, ir, symbols, adjusted)
                    targets.append((ref, unit, sym.pin(str(pin_no))))
            for ref, unit, pin in targets:
                place = adjusted[ref][unit]
                pos = pin_absolute_position(place, pin)
                if _pin_on_grid(pos):
                    continue
                snapped = snap_pin_to_grid(place, pin)
                if snapped is None:
                    continue
                if snapped == place:
                    continue
                if _apply_place(adjusted, ref, unit, snapped, ir, symbols):
                    notes.append(
                        f"route-place: snapped {ref} for off_grid on {net_name}"
                    )
            continue

        if failure.reason in ("escape_blocked", "foreign_geometry"):
            for ref, unit, pin in endpoints:
                place = adjusted[ref][unit]
                for cand in _nudge_candidates(place, pin):
                    if _apply_place(adjusted, ref, unit, cand, ir, symbols):
                        notes.append(
                            f"route-place: nudged {ref} for {failure.reason} "
                            f"on {net_name}"
                        )
                        break
            continue

        if failure.reason == "astar_no_path":
            if len(endpoints) < 2:
                # Push the one movable part away from the first non-movable peer.
                net = next((n for n in ir.nets if n.name == net_name), None)
                if net is None or not endpoints:
                    continue
                ref, unit, pin = endpoints[0]
                place = adjusted[ref][unit]
                from_pos = pin_absolute_position(place, pin)
                peer_pos = None
                for pref, ppin in net.nodes:
                    if pref == ref:
                        continue
                    if pref not in adjusted:
                        continue
                    psym = symbols.get(ir.components[pref].lib_id)
                    if psym is None:
                        continue
                    pu = _unit_for(pref, ppin, ir, symbols, adjusted)
                    peer_pos = pin_absolute_position(
                        adjusted[pref][pu], psym.pin(str(ppin))
                    )
                    break
                if peer_pos is None:
                    continue
                for cand in _push_apart_candidates(place, from_pos, peer_pos):
                    # Move away from peer: invert direction relative to peer.
                    away = Placement(
                        round(place.x - (cand.x - place.x), 4),
                        round(place.y - (cand.y - place.y), 4),
                        place.rotation,
                        place.mirror,
                    )
                    if _apply_place(adjusted, ref, unit, away, ir, symbols):
                        notes.append(
                            f"route-place: separated {ref} for astar_no_path "
                            f"on {net_name}"
                        )
                        break
                continue
            (r1, u1, p1), (r2, u2, p2) = endpoints[0], endpoints[1]
            pos1 = pin_absolute_position(adjusted[r1][u1], p1)
            pos2 = pin_absolute_position(adjusted[r2][u2], p2)
            moved = False
            for cand in _push_apart_candidates(adjusted[r1][u1], pos1, pos2):
                away = Placement(
                    round(adjusted[r1][u1].x - (cand.x - adjusted[r1][u1].x), 4),
                    round(adjusted[r1][u1].y - (cand.y - adjusted[r1][u1].y), 4),
                    adjusted[r1][u1].rotation,
                    adjusted[r1][u1].mirror,
                )
                if _apply_place(adjusted, r1, u1, away, ir, symbols):
                    notes.append(
                        f"route-place: separated {r1} for astar_no_path on {net_name}"
                    )
                    moved = True
                    break
            if not moved:
                for cand in _push_apart_candidates(adjusted[r2][u2], pos2, pos1):
                    away = Placement(
                        round(adjusted[r2][u2].x - (cand.x - adjusted[r2][u2].x), 4),
                        round(adjusted[r2][u2].y - (cand.y - adjusted[r2][u2].y), 4),
                        adjusted[r2][u2].rotation,
                        adjusted[r2][u2].mirror,
                    )
                    if _apply_place(adjusted, r2, u2, away, ir, symbols):
                        notes.append(
                            f"route-place: separated {r2} for astar_no_path "
                            f"on {net_name}"
                        )
                        break

    return adjusted, notes


def placements_equal(
    a: dict[str, dict[int, Placement]],
    b: dict[str, dict[int, Placement]],
) -> bool:
    if a.keys() != b.keys():
        return False
    for ref in a:
        if a[ref].keys() != b[ref].keys():
            return False
        for unit in a[ref]:
            if a[ref][unit] != b[ref][unit]:
                return False
    return True
