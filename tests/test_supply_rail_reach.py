"""Requested supply rails vs which nets device PWRIN pins actually reach.

Synthetic SymbolDefs only — no campaign prompts, no part-family specials.
"""

from circuitgen.compliance import check_compliance, check_requested_rail_reach
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib_id, pins, *, is_power=False, ref="U"):
    return SymbolDef(
        lib_id,
        f'(symbol "{lib_id.split(":")[-1]}")',
        [
            PinDef(str(n), name, etype, 0, 0, 0, 2.54)
            for n, name, etype in pins
        ],
        is_power=is_power,
        reference_prefix=ref,
    )


def _rail(name):
    lib = f"power:{name}"
    return lib, _sym(lib, [("1", name, PinType.PWRIN)], is_power=True, ref="#PWR")


def test_opamp_vdd_on_requested_3v3_matches():
    opamp = "Amplifier_Operational:TestOpAmp"
    lib_3v3, sym_3v3 = _rail("+3V3")
    lib_gnd, sym_gnd = _rail("GND")
    symbols = {
        opamp: _sym(opamp, [
            ("4", "VSS", PinType.PWRIN),
            ("8", "VDD", PinType.PWRIN),
            ("2", "-", PinType.INPUT),
            ("3", "+", PinType.INPUT),
            ("1", "OUT", PinType.OUTPUT),
        ]),
        lib_3v3: sym_3v3,
        lib_gnd: sym_gnd,
    }
    ir = CircuitIR("reach_ok")
    ir.add(Component("U1", opamp, "TestOpAmp"))
    ir.add(Component("#PWR01", lib_3v3, "+3V3"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.connect("+3V3", ("U1", "8"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "4"), ("#PWR02", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    by_pin = {(r["reference"], r["pin"]): r for r in records}
    assert by_pin[("U1", "8")]["match"] is True
    assert by_pin[("U1", "8")]["reason"] == "reaches_requested_rail"
    assert by_pin[("U1", "4")]["match"] is True
    assert not any(i.rule == "requested_rail_absent" for i in issues)
    assert not any(i.rule == "power_pin_misses_requested_rail" for i in issues)


def test_unconnected_regulator_vi_records_unconnected_without_miss_issue():
    ldo = "Regulator_Linear:TestLDO-3.3"
    symbols = {
        ldo: _sym(ldo, [
            ("1", "VI", PinType.PWRIN),
            ("2", "GND", PinType.PWRIN),
            ("3", "VO", PinType.PWROUT),
        ]),
        **{lib: sym for lib, sym in (_rail("+5V"), _rail("+3V3"), _rail("GND"))},
    }
    ir = CircuitIR("unconnected_vi")
    ir.add(Component("U1", ldo, "TestLDO-3.3"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR03", "power:GND", "GND"))
    ir.connect("GND", ("U1", "2"), ("#PWR03", "1"))
    ir.connect("+5V", ("#PWR01", "1"))
    ir.connect("+3V3", ("U1", "3"), ("#PWR02", "1"))
    # VI left unconnected — power_pin_unpowered is check_power_integrity's job
    spec = {"power": {"rails": [
        {"name": "+5V", "voltage": "5V"},
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    vi = next(r for r in records if r["pin"] == "1")
    assert vi["match"] is False
    assert vi["reason"] == "unconnected"
    assert not any(
        i.rule == "power_pin_misses_requested_rail" and i.path == "U1.1"
        for i in issues
    )


def test_ic_on_unrequested_12v_emits_miss():
    ic = "Amplifier_Operational:TestOpAmp"
    lib_12, sym_12 = _rail("+12V")
    lib_gnd, sym_gnd = _rail("GND")
    symbols = {
        ic: _sym(ic, [("8", "VDD", PinType.PWRIN), ("4", "VSS", PinType.PWRIN)]),
        lib_12: sym_12,
        lib_gnd: sym_gnd,
        "power:+5V": _rail("+5V")[1],
        "power:+3V3": _rail("+3V3")[1],
    }
    ir = CircuitIR("wrong_rail")
    ir.add(Component("U1", ic, "TestOpAmp"))
    ir.add(Component("#PWR01", lib_12, "+12V"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.add(Component("#PWR03", "power:+5V", "+5V"))
    ir.add(Component("#PWR04", "power:+3V3", "+3V3"))
    ir.connect("+12V", ("U1", "8"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "4"), ("#PWR02", "1"))
    ir.connect("+5V", ("#PWR03", "1"))
    ir.connect("+3V3", ("#PWR04", "1"))
    spec = {"power": {"rails": [
        {"name": "+5V", "voltage": "5V"},
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    vdd = next(r for r in records if r["pin"] == "8")
    assert vdd["match"] is False
    assert vdd["reason"] == "not_requested_rail"
    assert any(
        i.rule == "power_pin_misses_requested_rail" and i.path == "U1.8"
        for i in issues
    )


def test_requested_rail_absent_when_no_net_or_power_symbol():
    ic = "MCU_Generic:TestMCU"
    lib_gnd, sym_gnd = _rail("GND")
    symbols = {
        ic: _sym(ic, [("1", "VDD", PinType.PWRIN), ("2", "VSS", PinType.PWRIN)]),
        lib_gnd: sym_gnd,
    }
    ir = CircuitIR("missing_rail")
    ir.add(Component("U1", ic, "TestMCU"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.connect("GND", ("U1", "2"), ("#PWR02", "1"))
    # VDD on a signal net so the pin is connected but +3V3 is absent
    ir.connect("GPIO0", ("U1", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    absent = [i for i in issues if i.rule == "requested_rail_absent" and "+3V3" in i.message]
    assert absent and absent[0].severity == "error"
    vdd = next(r for r in records if r["pin"] == "1")
    assert vdd["match"] is False


def test_phantom_absent_rail_is_warning_not_error():
    """Extractor paraphrases (+0V25, +2V) must not count as board-defect errors."""
    ic = "MCU_Generic:TestMCU"
    lib_gnd, sym_gnd = _rail("GND")
    lib_3v3, sym_3v3 = _rail("+3V3")
    symbols = {
        ic: _sym(ic, [("1", "VDD", PinType.PWRIN), ("2", "VSS", PinType.PWRIN)]),
        lib_gnd: sym_gnd,
        lib_3v3: sym_3v3,
    }
    ir = CircuitIR("phantom_rails")
    ir.add(Component("U1", ic, "TestMCU"))
    ir.add(Component("#PWR01", lib_3v3, "+3V3"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.connect("+3V3", ("U1", "1"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "2"), ("#PWR02", "1"))

    for phantom in ("+0V25", "+2V"):
        spec = {"power": {"rails": [
            {"name": "+3V3", "voltage": "3.3V"},
            {"name": phantom, "voltage": phantom},
            {"name": "GND", "voltage": "0V"},
        ]}}
        issues, _records = check_requested_rail_reach(ir, symbols, spec)
        hit = [i for i in issues if i.rule == "requested_rail_absent" and i.path == f"rail:{phantom}"]
        assert len(hit) == 1, phantom
        assert hit[0].severity == "warning", phantom
        assert "may not be a board supply" in hit[0].message, phantom


def test_two_power_domains_both_match_requested_rails():
    """Same contract for an audio-style VS domain and an MCU-style VDD domain."""
    amp = "Amplifier_Audio:TestAmp"
    mcu = "MCU_Generic:TestMCU"
    symbols = {
        amp: _sym(amp, [("7", "VS", PinType.PWRIN), ("4", "GND", PinType.PWRIN)]),
        mcu: _sym(mcu, [("1", "VDD", PinType.PWRIN), ("2", "VSS", PinType.PWRIN)]),
        **{lib: sym for lib, sym in (_rail("+9V"), _rail("+3V3"), _rail("GND"))},
    }
    ir = CircuitIR("two_domains")
    ir.add(Component("U1", amp, "TestAmp"))
    ir.add(Component("U2", mcu, "TestMCU"))
    ir.add(Component("#PWR01", "power:+9V", "+9V"))
    ir.add(Component("#PWR02", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR03", "power:GND", "GND"))
    ir.connect("+9V", ("U1", "7"), ("#PWR01", "1"))
    ir.connect("+3V3", ("U2", "1"), ("#PWR02", "1"))
    ir.connect("GND", ("U1", "4"), ("U2", "2"), ("#PWR03", "1"))
    spec = {"power": {"rails": [
        {"name": "+9V", "voltage": "9V"},
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    by_ref_pin = {(r["reference"], r["pin"]): r for r in records}
    assert by_ref_pin[("U1", "7")]["match"] is True  # audio VS → +9V
    assert by_ref_pin[("U2", "1")]["match"] is True  # mcu VDD → +3V3
    assert not any(i.rule == "power_pin_misses_requested_rail" for i in issues)
    assert not any(i.rule == "requested_rail_absent" for i in issues)


def test_named_gnd_pin_on_gnd_matches():
    """GND / VSS pins on GND match; VI on GND is still a miss."""
    ldo = "Regulator_Linear:TestLDO-3.3"
    symbols = {
        ldo: _sym(ldo, [
            ("3", "VI", PinType.PWRIN),
            ("1", "GND", PinType.PWRIN),
            ("2", "VSS", PinType.PWRIN),
        ]),
        **{lib: sym for lib, sym in (_rail("+3V3"), _rail("GND"))},
    }
    ir = CircuitIR("gnd_pin_ok")
    ir.add(Component("U2", ldo, "TestLDO-3.3"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+3V3", ("#PWR01", "1"))
    ir.connect("GND", ("U2", "1"), ("U2", "2"), ("U2", "3"), ("#PWR02", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    by = {(r["reference"], r["pin"]): r for r in records}
    assert by[("U2", "1")]["match"] is True  # GND pin → GND
    assert by[("U2", "2")]["match"] is True  # VSS (is_ground_pin) → GND
    assert by[("U2", "3")]["match"] is False  # VI → GND is wrong
    assert by[("U2", "3")]["reason"] == "not_requested_rail"
    assert any(
        i.rule == "power_pin_misses_requested_rail" and i.path == "U2.3"
        for i in issues
    )


def test_vminus_on_gnd_is_not_auto_accepted():
    """V- on GND with unused -12V present must not match (dual-supply bug)."""
    opamp = "Amplifier_Operational:TestOpAmp"
    symbols = {
        opamp: _sym(opamp, [
            ("4", "V-", PinType.PWRIN),
            ("8", "V+", PinType.PWRIN),
        ]),
        **{lib: sym for lib, sym in (
            _rail("+12V"), _rail("-12V"), _rail("GND"),
        )},
    }
    ir = CircuitIR("vminus_not_gnd")
    ir.add(Component("U1", opamp, "TestOpAmp"))
    ir.add(Component("#PWR01", "power:+12V", "+12V"))
    ir.add(Component("#PWR02", "power:-12V", "-12V"))
    ir.add(Component("#PWR03", "power:GND", "GND"))
    ir.connect("+12V", ("U1", "8"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "4"), ("#PWR03", "1"))
    ir.connect("-12V", ("#PWR02", "1"))  # present on board, unused
    spec = {"power": {"rails": [
        {"name": "+12V", "voltage": "12V"},
        {"name": "-12V", "voltage": "-12V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    by = {(r["reference"], r["pin"]): r for r in records}
    assert by[("U1", "4")]["match"] is False
    assert by[("U1", "4")]["reason"] == "not_requested_rail"
    assert any(
        i.rule == "power_pin_misses_requested_rail" and i.path == "U1.4"
        for i in issues
    )


def test_ground_pin_miss_message_lists_ground_rails():
    ldo = "Regulator_Linear:TestLDO-3.3"
    symbols = {
        ldo: _sym(ldo, [
            ("1", "GND", PinType.PWRIN),
            ("2", "VI", PinType.PWRIN),
            ("3", "VO", PinType.PWROUT),
        ]),
        **{lib: sym for lib, sym in (
            _rail("+5V"), _rail("+3V3"), _rail("+12V"), _rail("GND"),
        )},
    }
    ir = CircuitIR("gnd_msg")
    ir.add(Component("U3", ldo, "TestLDO-3.3"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR03", "power:GND", "GND"))
    ir.add(Component("#PWR04", "power:+12V", "+12V"))
    # GND pin on an unrequested power rail (not via reaches_supply)
    ir.connect("+12V", ("U3", "1"), ("#PWR04", "1"))
    ir.connect("+5V", ("U3", "2"), ("#PWR01", "1"))
    ir.connect("+3V3", ("U3", "3"), ("#PWR02", "1"))
    ir.connect("GND", ("#PWR03", "1"))
    spec = {"power": {"rails": [
        {"name": "+5V", "voltage": "5V"},
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, _records = check_requested_rail_reach(ir, symbols, spec)
    miss = next(
        i for i in issues
        if i.rule == "power_pin_misses_requested_rail" and i.path == "U3.1"
    )
    assert "requested ground rails" in miss.message
    assert "GND" in miss.message


def test_conceptual_placeholder_emits_unverifiable_warning():
    symbols = {
        "Device:R": _sym("Device:R", [("1", "", PinType.PASSIVE), ("2", "", PinType.PASSIVE)], ref="R"),
        **{lib: sym for lib, sym in (_rail("+5V"), _rail("GND"))},
    }
    ir = CircuitIR("conceptual_silence")
    ir.add(Component("U1", "Conceptual:NE555D", "NE555D"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+5V", ("#PWR01", "1"), ("R1", "1"))
    ir.connect("GND", ("#PWR02", "1"), ("R1", "2"))
    spec = {"power": {"rails": [
        {"name": "+5V", "voltage": "5V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    issues, records = check_requested_rail_reach(ir, symbols, spec)
    assert records == []
    assert any(
        i.rule == "supply_rail_reach_unverifiable" and i.severity == "warning"
        for i in issues
    )


def test_check_compliance_includes_supply_rail_reach_in_as_dict():
    ic = "MCU_Generic:TestMCU"
    lib_3v3, sym_3v3 = _rail("+3V3")
    lib_gnd, sym_gnd = _rail("GND")
    symbols = {
        ic: _sym(ic, [("1", "VDD", PinType.PWRIN), ("2", "VSS", PinType.PWRIN)]),
        lib_3v3: sym_3v3,
        lib_gnd: sym_gnd,
    }
    ir = CircuitIR("compliance_dict")
    ir.add(Component("U1", ic, "TestMCU", "F:F"))
    ir.add(Component("#PWR01", lib_3v3, "+3V3"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.connect("+3V3", ("U1", "1"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "2"), ("#PWR02", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    report = check_compliance(ir, symbols, prompt="", spec=spec)
    payload = report.as_dict()
    assert "supply_rail_reach" in payload
    assert payload["supply_rail_reach"]
    assert any(item["match"] is True for item in payload["supply_rail_reach"])


def test_alias_vcc_net_merges_into_existing_requested_3v3():
    """024: flash VCC and C on +3V3 were two power nets for one requested rail."""
    from circuitgen.agent import _reconcile_rails
    from circuitgen.compliance import check_requested_rail_reach

    flash = "Memory_Flash:W25Q32JVSS"
    lib_3v3, sym_3v3 = _rail("+3V3")
    lib_gnd, sym_gnd = _rail("GND")
    lib_vcc, sym_vcc = _rail("VCC")
    symbols = {
        flash: _sym(flash, [
            ("8", "VCC", PinType.PWRIN),
            ("4", "VSS", PinType.PWRIN),
        ]),
        "Device:C": _sym("Device:C", [
            ("1", "1", PinType.PASSIVE),
            ("2", "2", PinType.PASSIVE),
        ]),
        lib_3v3: sym_3v3,
        lib_gnd: sym_gnd,
        lib_vcc: sym_vcc,
    }
    ir = CircuitIR("vcc-alias")
    ir.add(Component("U1", flash, "W25Q32JVSS"))
    ir.add(Component("C1", "Device:C", "100nF"))
    ir.add(Component("#PWR01", lib_3v3, "+3V3"))
    ir.add(Component("#PWR02", lib_gnd, "GND"))
    ir.add(Component("#PWR03", lib_vcc, "VCC"))
    ir.connect("+3V3", ("C1", "1"), ("#PWR01", "1"))
    ir.connect("GND", ("C1", "2"), ("U1", "4"), ("#PWR02", "1"))
    ir.connect("VCC", ("U1", "8"), ("#PWR03", "1"))
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"},
        {"name": "GND", "voltage": "0V"},
    ]}}

    _issues, before = check_requested_rail_reach(ir, symbols, spec)
    assert any(
        r["reference"] == "U1" and r["reason"] == "not_requested_rail"
        for r in before
    )
    notes = _reconcile_rails(ir, spec)
    assert any("merged alias net 'VCC'" in n for n in notes), notes
    assert not any(n.name == "VCC" for n in ir.nets)
    assert ("U1", "8") in next(n for n in ir.nets if n.name == "+3V3").nodes
    assert ("C1", "1") in next(n for n in ir.nets if n.name == "+3V3").nodes
    _issues, after = check_requested_rail_reach(ir, symbols, spec)
    assert all(
        r["match"] for r in after if r["reference"] == "U1"
    )
    assert _reconcile_rails(ir, spec) == []


def test_agnd_is_not_folded_into_gnd():
    from circuitgen.agent import _reconcile_rails

    ir = CircuitIR("agnd")
    ir.add(Component("U1", "Vendor:CHIP", "x"))
    ir.connect("GND", ("U1", "1"))
    ir.connect("AGND", ("U1", "2"))
    spec = {"power": {"rails": [{"name": "GND", "voltage": "0V"}]}}
    notes = _reconcile_rails(ir, spec)
    assert not any("merged" in n for n in notes)
    assert {n.name for n in ir.nets} == {"GND", "AGND"}
