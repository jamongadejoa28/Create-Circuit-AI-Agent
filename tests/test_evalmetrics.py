"""The measurement layer the direction doc (§6) asks for.

These metrics deliberately gate nothing. A number that decides pass/fail
invites being optimised against, which is exactly the failure this module
exists to expose: the old release score was dominated by the ERC family and
could not say which circuit family failed or why.
"""

from circuitgen.evalmetrics import (
    connection_set,
    diff_connections,
    measure_run,
    nc_set,
    role_fulfilment,
    summarize,
)
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib_id, n=2, power=False):
    s = SymbolDef(
        lib_id, "",
        [PinDef(str(i), f"P{i}", PinType.PASSIVE, 0, 0, 0, 2.54) for i in range(1, n + 1)],
    )
    s.is_power = power
    return s


SPEC = {"parts_needed": [
    {"role": "microcontroller", "search_query": "STM32 microcontroller"},
    {"role": "I2C temperature sensor", "search_query": "I2C temperature sensor"},
    {"role": "SDA pull-up", "search_query": "resistor", "value": "10k"},
]}


def test_a_role_is_missing_only_when_we_could_actually_check():
    """A verdict needs a warrant. The synonym table that used to answer here
    reported an MCP6001 board as missing its op-amp and an STM32 board as
    missing its MCU, because 'opamp' is not a substring of 'MCP6001R' and the
    table had no entry for either word."""
    ir = CircuitIR("m")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx"))
    ir.add(Component("R1", "Device:R", "10k"))
    symbols = {
        "MCU_ST_STM32G4:STM32G474RETx": _sym("MCU_ST_STM32G4:STM32G474RETx", 8),
        "Device:R": _sym("Device:R"),
    }
    candidates = {"I2C temperature sensor": [{"lib_id": "Sensor_Temperature:TMP100"}]}
    total, present, missing, _short, unver = role_fulfilment(SPEC, ir, symbols, candidates)
    assert total == 3
    # STM32 matches by token; the sensor had candidates offered and none is on
    # the board, so it is genuinely missing
    assert missing == ["I2C temperature sensor"]
    # the pull-up role names nothing in the circuit and had no candidates
    # recorded, so we cannot tell — that is not the same as absent
    assert unver == ["SDA pull-up"] and present == 1


def test_power_symbols_do_not_count_as_fulfilling_a_role():
    ir = CircuitIR("m")
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    symbols = {"power:+3V3": _sym("power:+3V3", 1, power=True)}
    total, present, missing, _s, unver = role_fulfilment(SPEC, ir, symbols)
    assert (total, present) == (3, 0) and len(missing) + len(unver) == 3


def test_quantity_shortfall_is_reported_separately_from_presence():
    spec = {"parts_needed": [{"role": "encoder", "search_query": "encoder", "quantity": 4}]}
    ir = CircuitIR("m")
    ir.add(Component("U1", "Vendor:ENCODER", "ENCODER"))
    symbols = {"Vendor:ENCODER": _sym("Vendor:ENCODER", 8)}
    total, present, missing, shortfall, _u = role_fulfilment(spec, ir, symbols)
    assert (total, present, missing) == (1, 1, [])
    assert shortfall == {"encoder": 3}, "present but 3 short of the requested 4"


def test_automatic_connections_are_measured_from_the_ir_not_from_log_prose():
    """The first version matched fifteen substrings against note text, which
    measures how passes phrase themselves. The set difference is exact."""
    before = CircuitIR("b")
    before.add(Component("U1", "Vendor:CHIP", "CHIP"))
    before.connect("SDA", ("U1", "9"))

    after = CircuitIR("a")
    after.add(Component("U1", "Vendor:CHIP", "CHIP"))
    after.connect("SDA", ("U1", "9"))
    after.connect("+3V3", ("U1", "1"), ("U1", "16"))   # wired by a device rule
    after.connect("GND", ("U1", "15"))
    after.nc_pins.extend([("U1", "20"), ("U1", "21")])  # closed by code

    diff = diff_connections(
        connection_set(before), connection_set(after), nc_set(before), nc_set(after)
    )
    assert diff["added_connections"] == 3
    assert diff["added_no_connects"] == 2
    assert diff["by_component"] == {"U1": 3}
    assert "+3V3:U1.16" in diff["samples"]


def test_a_connection_the_model_made_is_not_counted_as_automatic():
    ir = CircuitIR("x")
    ir.add(Component("U1", "Vendor:CHIP", "CHIP"))
    ir.connect("SDA", ("U1", "9"))
    same = connection_set(ir)
    assert diff_connections(same, same, set(), set())["added_connections"] == 0


def test_measure_run_survives_a_run_that_produced_no_circuit():
    metrics = measure_run(SPEC, None, {}, None)
    assert metrics.role_total == 0 and metrics.role_fulfilment is None
    assert metrics.as_dict()["auto_connections"] == 0


def test_summary_reports_per_family_with_repeat_spread():
    rows = [
        {"domain": "sensor_bus", "kicad_violations": 0, "self_erc_errors": 0,
         "connectivity_ok": True, "compliance_ok": True, "visual_issues": 0,
         "stage": "done", "wiring": {"wired_ratio": 0.8},
         "metrics": {"role_fulfilment": 1.0, "auto_connections": 4}},
        {"domain": "sensor_bus", "kicad_violations": 12, "self_erc_errors": 3,
         "connectivity_ok": True, "compliance_ok": False, "visual_issues": 1,
         "stage": "repair-2", "wiring": {"wired_ratio": 0.2},
         "metrics": {"role_fulfilment": 0.5, "auto_connections": 40}},
    ]
    out = summarize(rows)["sensor_bus"]
    assert out["runs"] == 2 and out["erc_clean"] == 1 and out["compliance_ok"] == 1
    # the variance across repeats is the point: one green run proves nothing
    assert out["kicad_violations"]["spread"] == 12
    assert out["wired_ratio"]["min"] == 0.2 and out["wired_ratio"]["max"] == 0.8
    assert out["auto_connections"]["max"] == 40
    assert sorted(out["stages"]) == ["done", "repair-2"]
