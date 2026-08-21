"""Generic rule-graph compiler mechanics; no prompt-keyword design fixtures."""

from pathlib import Path

import pytest

from circuitgen.ir import CircuitIR
from circuitgen.kicad_cli import KICAD_CLI
from circuitgen.patterns import (
    PatternBinding,
    bind_role_pins,
    instantiate_pattern,
    validate_pattern,
    verify_pattern_instance,
)
from circuitgen.symbols import KICAD_SYMBOL_DIR, load_symbols

oracle = pytest.mark.skipif(
    not (Path(KICAD_CLI).exists() and KICAD_SYMBOL_DIR.exists()),
    reason="kicad-cli.exe / bundled libraries not available",
)

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
