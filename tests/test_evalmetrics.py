"""The measurement layer the direction doc (§6) asks for.

These metrics deliberately gate nothing. A number that decides pass/fail
invites being optimised against, which is exactly the failure this module
exists to expose: the old release score was dominated by the ERC family and
could not say which circuit family failed or why.
"""

from circuitgen.evalmetrics import (
    count_auto_connections,
    measure_run,
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


def test_role_fulfilment_counts_what_is_actually_on_the_board():
    ir = CircuitIR("m")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx"))
    ir.add(Component("R1", "Device:R", "10k"))
    symbols = {
        "MCU_ST_STM32G4:STM32G474RETx": _sym("MCU_ST_STM32G4:STM32G474RETx", 8),
        "Device:R": _sym("Device:R"),
    }
    total, present, missing, _short = role_fulfilment(SPEC, ir, symbols)
    assert total == 3 and present == 2
    assert missing == ["I2C temperature sensor"], "the absent sensor must be named"


def test_power_symbols_do_not_count_as_fulfilling_a_role():
    ir = CircuitIR("m")
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    symbols = {"power:+3V3": _sym("power:+3V3", 1, power=True)}
    total, present, missing, _s = role_fulfilment(SPEC, ir, symbols)
    assert (total, present) == (3, 0) and len(missing) == 3


def test_quantity_shortfall_is_reported_separately_from_presence():
    spec = {"parts_needed": [{"role": "encoder", "search_query": "encoder", "quantity": 4}]}
    ir = CircuitIR("m")
    ir.add(Component("U1", "Vendor:ENCODER", "ENCODER"))
    symbols = {"Vendor:ENCODER": _sym("Vendor:ENCODER", 8)}
    total, present, missing, shortfall = role_fulfilment(spec, ir, symbols)
    assert (total, present, missing) == (1, 1, [])
    assert shortfall == {"encoder": 3}, "present but 3 short of the requested 4"


def test_automatic_connections_are_counted_and_refusals_are_not():
    log = [
        "connected U1.16 to +3V3",
        "closed 104 unused pin(s) of U1 (CPU:MC68332) as NC: 29, 30",
        "added rail +3V3: STM32G474 operates at 1.71-3.6 V",
        "U1 (CPU:MC68332): 13 supply pin(s) left unconnected — no datasheet limits are recorded",
        "pattern relay_driver declined: the request also asks for ethernet",
        "pattern match: i2c_temperature_sensor",
    ]
    made, notes, refused = count_auto_connections(log)
    assert made == 3, notes
    assert refused == 2, "a refusal is the good outcome and must not inflate the count"


def test_measure_run_survives_a_run_that_produced_no_circuit():
    metrics = measure_run(SPEC, None, {}, ["requirement extraction failed: boom"])
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
