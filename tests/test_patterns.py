"""CircuitPattern engine: schema validation, binding, instantiation, and an
oracle end-to-end — a textbook non-inverting amplifier pattern bound to real
KiCad parts must survive place/emit with KiCad ERC 0 and an identical
netlist round-trip, with its feedback network drawn as REAL wires."""

from pathlib import Path

import pytest

from circuitgen.emit import build_emit_plan, emit_schematic, normalize_placements, route_metrics
from circuitgen.ir import CircuitIR, Component
from circuitgen.kicad_cli import KICAD_CLI, export_netlist, run_erc
from circuitgen.netlist import compare_connectivity
from circuitgen.patterns import (
    PATTERN_DIR,
    PatternBinding,
    bind_role_pins,
    instantiate_pattern,
    load_patterns,
    match_patterns,
    validate_pattern,
    verify_pattern_instance,
)
from circuitgen.place import heuristic_place
from circuitgen.project import write_project
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

oracle = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

OUT = Path(__file__).resolve().parent / "artifacts" / "generated" / "patterns"
FIXTURE_PATTERN_DIR = Path(__file__).resolve().parent / "fixtures" / "patterns"


def test_seed_patterns_load_and_validate():
    patterns = load_patterns(PATTERN_DIR)
    assert {
        "noninverting_amplifier", "inverting_amplifier",
        "rc_lowpass_filter", "relay_driver",
    } <= set(patterns)
    assert not any(
        p["source"].get("provenance") == "internal-fixture"
        for p in patterns.values()
    )
    for p in patterns.values():
        assert validate_pattern(p) == []
        assert p["source"]["book"] and p["source"]["section"]


def test_validate_rejects_broken_patterns():
    broken = {
        "id": "x", "roles": {"R": {"kind": "resistor"}}, "ports": ["VIN"],
        "topology": [["VIN", "R.9"]],
        "source": {"book": "b", "section": "s"}, "status": "draft",
    }
    assert any("invalid endpoint" in e for e in validate_pattern(broken))
    uncited = {
        "id": "y", "roles": {}, "ports": [], "topology": [],
        "source": {"book": "", "section": ""}, "status": "draft",
    }
    assert any("source.book" in e for e in validate_pattern(uncited))

    unsafe_partial = {
        "id": "z", "roles": {"R": {"kind": "resistor", "allow_unbound_pins": True}},
        "ports": [], "topology": [], "source": {"book": "b", "section": "s"},
        "status": "draft",
    }
    assert any("explicit hub roles" in e for e in validate_pattern(unsafe_partial))

    internal = {
        "id": "fixture", "roles": {}, "ports": [], "topology": [],
        "source": {
            "book": "test output", "section": "fixture",
            "provenance": "internal-fixture",
        },
        "status": "verified",
    }
    assert any("test artifacts" in e for e in validate_pattern(internal))
    assert validate_pattern(internal, allow_internal_fixtures=True) == []


def test_match_patterns_by_keyword():
    patterns = load_patterns(PATTERN_DIR)
    hits = match_patterns("5V 비반전 증폭 회로를 만들어줘", patterns)
    assert [p["id"] for p in hits] == ["noninverting_amplifier"]
    assert match_patterns("just an MCU board", patterns) == []
    assert match_patterns(
        "MCU에 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함", patterns
    ) == []
    fixtures = load_patterns(FIXTURE_PATTERN_DIR, allow_internal_fixtures=True)
    assert [p["id"] for p in match_patterns(
        "MCU에 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함", fixtures
    )] == ["i2c_temperature_sensor"]


def bind_pattern(pattern, role_symbols):
    """Test fixture: bind every role at once.

    Production binds one role at a time (agent._pattern_synthesis calls
    bind_role_pins per role while it searches for a symbol that fits), so this
    all-at-once wrapper lived in patterns.py without a caller. It is a test
    convenience, so it lives with the tests.
    """
    binding, errors = PatternBinding(), []
    for role in pattern["roles"]:
        if role not in role_symbols:
            errors.append(f"role {role}: no symbol supplied")
            continue
        lib_id, sym = role_symbols[role]
        pins = bind_role_pins(pattern, role, sym)
        if pins is None:
            errors.append(f"role {role}: pins unresolved on {lib_id}")
            continue
        binding.lib_ids[role], binding.pins[role] = lib_id, pins
    return (None, errors) if errors else (binding, [])


@oracle
def test_i2c_pattern_binds_verified_mcu_sensor_pair():
    pattern = load_patterns(
        FIXTURE_PATTERN_DIR, allow_internal_fixtures=True
    )["i2c_temperature_sensor"]
    lib_ids = {role: spec["lib_id"] for role, spec in pattern["roles"].items()}
    symbols = load_symbols(sorted(set(lib_ids.values())))
    binding, errors = bind_pattern(
        pattern, {role: (lid, symbols[lid]) for role, lid in lib_ids.items()}
    )
    assert errors == [], errors
    assert binding.pins["MCU"] == {"SDA": "62", "SCL": "51"}
    assert binding.pins["SENSOR"] == {"SDA": "1", "SCL": "6", "VDD": "5", "GND": "2"}

    ir = CircuitIR("i2c_pattern")
    refs = {"MCU": "U1", "SENSOR": "U2", "R_SDA": "R1", "R_SCL": "R2", "C_SENSOR": "C1"}
    instantiate_pattern(ir, pattern, binding, refs, {"VCC": "+3V3"})
    assert verify_pattern_instance(ir, pattern, binding, refs, {"VCC": "+3V3"}) == []
    nets = {n.name: set(n.nodes) for n in ir.nets}
    assert {("U1", "62"), ("U2", "1"), ("R1", "1")} <= nets["MCU_SDA"]
    assert {("U1", "51"), ("U2", "6"), ("R2", "1")} <= nets["MCU_SCL"]


@oracle
def test_noninverting_amplifier_binds_instantiates_and_roundtrips():
    patterns = load_patterns(PATTERN_DIR)
    pattern = patterns["noninverting_amplifier"]
    lib_ids = {
        "AMP": "Amplifier_Operational:MCP6001-OT",
        "RF": "Device:R",
        "RG": "Device:R",
    }
    symbols = load_symbols(sorted(set(lib_ids.values())))
    binding, errors = bind_pattern(
        pattern, {role: (lid, symbols[lid]) for role, lid in lib_ids.items()}
    )
    assert errors == [] and binding is not None
    # blank-named MCP6001 output resolved via unique OUTPUT etype
    assert binding.pins["AMP"]["OUT"] == "1"
    assert binding.pins["AMP"]["IN+"] == "3"
    assert binding.pins["AMP"]["IN-"] == "4"

    ir = CircuitIR("pat_t")
    refs = {"AMP": "U1", "RF": "R1", "RG": "R2"}
    ports = {"VCC": "+3V3"}
    notes = instantiate_pattern(
        ir, pattern, binding, refs, ports, values={"Rf": "100k", "Rg": "10k"}
    )
    assert len(notes) == 3
    assert verify_pattern_instance(ir, pattern, binding, refs, ports) == []
    assert ir.components["R1"].value == "100k"

    # complete the board: input connector, rails, flags
    ir.add(Component("J1", "Connector_Generic:Conn_01x02", "VIN",
                     "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "INPUT"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.add(Component("#FLG01", "power:PWR_FLAG", "PWR_FLAG"))
    ir.add(Component("#FLG02", "power:PWR_FLAG", "PWR_FLAG"))
    ir.connect("VIN", ("J1", "1"))
    ir.connect("GND", ("J1", "2"), ("#PWR02", "1"), ("#FLG02", "1"))
    ir.connect("+3V3", ("#PWR01", "1"), ("#FLG01", "1"))

    symbols = load_symbols(sorted({c.lib_id for c in ir.components.values()}))
    placements = heuristic_place(ir, symbols)
    plan = build_emit_plan(ir, symbols, normalize_placements(ir, symbols, placements))

    # the pattern's required solid paths: OUT->RF and the RF/RG/IN- node
    def net_of(ref, pin):
        return next(n.name for n in ir.nets if (ref, pin) in n.nodes)

    out_net = net_of("U1", binding.pins["AMP"]["OUT"])
    inv_net = net_of("U1", binding.pins["AMP"]["IN-"])
    assert plan.net_routes[out_net] in ("direct", "l", "tree"), plan.net_routes
    assert plan.net_routes[inv_net] in ("direct", "l", "tree"), plan.net_routes

    metrics = route_metrics(ir, symbols, plan)
    assert metrics["wired_nets"] >= 2 and metrics["signal_nets"] >= metrics["wired_nets"]

    OUT.mkdir(parents=True, exist_ok=True)
    text = emit_schematic(ir, symbols, placements)
    sch = OUT / "pat_t.kicad_sch"
    sch.write_text(text, encoding="utf-8")
    write_project(sch)

    erc = run_erc(sch)
    assert erc.ok, [v.get("type") for v in erc.violations]
    net = OUT / "pat_t.net"
    assert export_netlist(sch, net).returncode == 0
    ok, msg = compare_connectivity(ir, net)
    assert ok, msg


@oracle
def test_ldo_pattern_binds_ams1117():
    from circuitgen.rulegraph import load_rules, lower_to_pattern

    pattern = lower_to_pattern(load_rules()["ldo_linear_regulator"])
    lib_ids = {"REG": "Regulator_Linear:AMS1117-3.3", "CIN": "Device:C", "COUT": "Device:C"}
    symbols = load_symbols(sorted(set(lib_ids.values())))
    binding, errors = bind_pattern(
        pattern, {role: (lid, symbols[lid]) for role, lid in lib_ids.items()}
    )
    assert errors == []
    assert binding.pins["REG"] == {"IN": "3", "OUT": "2", "GND": "1"}

    ir = CircuitIR("ldo_t")
    refs = {"REG": "U1", "CIN": "C1", "COUT": "C2"}
    ports = {"VIN": "+12V", "VOUT": "+3V3"}
    instantiate_pattern(ir, pattern, binding, refs, ports, values={"Cin": "10uF", "Cout": "22uF"})
    assert verify_pattern_instance(ir, pattern, binding, refs, ports) == []
    plus12 = next(n for n in ir.nets if n.name == "+12V")
    assert ("U1", "3") in plus12.nodes and ("C1", "1") in plus12.nodes


@oracle
def test_relay_driver_binds_and_verifies():
    patterns = load_patterns(PATTERN_DIR)
    pattern = patterns["relay_driver"]
    lib_ids = {
        "Q": "Transistor_BJT:BC337",
        "RB": "Device:R",
        "D": "Device:D",
        "K1": "Relay:Relay_SPST-NO",
        "LOAD": "Connector_Generic:Conn_01x02",
    }
    symbols = load_symbols(sorted(set(lib_ids.values())))
    binding, errors = bind_pattern(
        pattern, {role: (lid, symbols[lid]) for role, lid in lib_ids.items()}
    )
    assert errors == [], errors
    assert binding.pins["Q"] == {"B": "2", "C": "1", "E": "3"}
    assert binding.pins["K1"] == {"A1": "A1", "A2": "A2", "13": "13", "14": "14"}

    ir = CircuitIR("relay_t")
    refs = {"Q": "Q1", "RB": "R1", "D": "D1", "K1": "K1", "LOAD": "J1"}
    ports = {"VCOIL": "+12V"}
    instantiate_pattern(ir, pattern, binding, refs, ports)
    assert verify_pattern_instance(ir, pattern, binding, refs, ports) == []
    coil_low = next(n for n in ir.nets if ("Q1", "1") in n.nodes)  # collector
    assert ("D1", "2") in coil_low.nodes and ("K1", "A2") in coil_low.nodes


@oracle
def test_led_switch_pattern_binds_and_verifies():
    patterns = load_patterns(FIXTURE_PATTERN_DIR, allow_internal_fixtures=True)
    pattern = patterns["led_switch_indicator"]
    lib_ids = {"SW": "Switch:SW_Push", "R": "Device:R", "D": "Device:LED"}
    symbols = load_symbols(sorted(set(lib_ids.values())))
    binding, errors = bind_pattern(
        pattern, {role: (lid, symbols[lid]) for role, lid in lib_ids.items()}
    )
    assert errors == [], errors
    ir = CircuitIR("led_t")
    refs = {"SW": "SW1", "R": "R1", "D": "D1"}
    ports = {"VCC": "+5V"}
    instantiate_pattern(ir, pattern, binding, refs, ports)
    assert verify_pattern_instance(ir, pattern, binding, refs, ports) == []
    # series chain: the switch is NOT across the rails
    nets = {n.name: n.nodes for n in ir.nets}
    assert ("SW1", "1") in nets["+5V"] and not any(
        ("SW1", p) in nets["GND"] for p in ("1", "2")
    )
    # keyword safety: 'led' substrings must not trigger
    assert pattern not in match_patterns("coupled inductor board", patterns)
    assert pattern in match_patterns("5V 전원에서 스위치로 켜는 LED 회로", patterns)
