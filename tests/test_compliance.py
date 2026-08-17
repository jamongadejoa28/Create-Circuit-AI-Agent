"""Requirement compliance + power integrity — the two questions ERC cannot answer.

Both rules exist because a board shipped as "ok" without them: an MCU whose
supply pins were all no-connect, and an STM32G474 with VDD on +5V, each at
KiCad ERC 0.
"""

import json
import pytest

from circuitgen.compliance import (
    DEVICE_LIMITS_PATH,
    check_compliance,
    check_power_integrity,
    check_requirements,
    ensure_device_supply_rails,
    load_device_limits,
    part_present,
    requested_part_numbers,
)
from circuitgen.erc import check_circuit
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.pins import PinType

STM32 = "MCU_ST_STM32G4:STM32G474RETx"


def sym(lib_id, pin_specs, is_power=False, ref="U"):
    return SymbolDef(
        lib_id=lib_id,
        raw_sexp=f'(symbol "{lib_id.split(":")[1]}")',
        pins=[
            PinDef(number=n, name=name, etype=t, x=0, y=0, orientation=0, length=1.27)
            for n, name, t in pin_specs
        ],
        is_power=is_power,
        reference_prefix=ref,
    )


SYMS = {
    STM32: sym(STM32, [
        ("1", "VDD", PinType.PWRIN),
        ("2", "VDDA", PinType.PWRIN),
        ("3", "VSS", PinType.PWRIN),
        ("4", "PB9", PinType.BIDIR),
    ]),
    "power:+3V3": sym("power:+3V3", [("1", "+3V3", PinType.PWRIN)], is_power=True, ref="#PWR"),
    "power:+5V": sym("power:+5V", [("1", "+5V", PinType.PWRIN)], is_power=True, ref="#PWR"),
    "power:GND": sym("power:GND", [("1", "GND", PinType.PWRIN)], is_power=True, ref="#PWR"),
    "power:PWR_FLAG": sym("power:PWR_FLAG", [("1", "pwr", PinType.PWROUT)], is_power=True, ref="#FLG"),
    "Device:R": sym("Device:R", [("1", "~", PinType.PASSIVE), ("2", "~", PinType.PASSIVE)], ref="R"),
}


def mcu_board(supply: str | None) -> CircuitIR:
    """STM32 + one pull-up. `supply` None means every VDD pin is marked NC."""
    ir = CircuitIR("compliance")
    ir.add(Component("U1", STM32, "STM32G474RETx", "F:F"))
    ir.add(Component("R1", "Device:R", "10k", "F:F"))
    rail = supply or "+3V3"
    ir.add(Component("#PWR01", f"power:{rail}", rail))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    for pin in ("1", "2"):
        if supply is None:
            ir.nc_pins.append(("U1", pin))
        else:
            ir.connect(supply, ("U1", pin))
    ir.connect("GND", ("U1", "3"))
    ir.connect("SDA", ("U1", "4"))
    ir.connect("SDA", ("R1", "1"))
    ir.connect(rail, ("R1", "2"))
    ir.connect(rail, ("#PWR01", "1"))
    ir.connect("GND", ("#PWR02", "1"))
    ensure_pwr_flags(ir, SYMS)
    return ir


# ---- requested part numbers -------------------------------------------------


def test_part_numbers_are_extracted_and_protocol_tokens_are_not():
    """A token is a part number when the CATALOG says so.

    This used to be a denylist of protocol and package names (RS485, IP65,
    USB20...), which is unbounded by construction and was written to stop
    specific false positives rather than from anything true about part numbers.
    """
    from circuitgen.partindex import PartIndex

    prompt = (
        "ESP32-C3에 BME280과 SHT30을 연결. RS485(Modbus RTU), CAN-FD, 24V, "
        "0805 저항, PA15/PB9 핀, I2C1 버스, IP65 케이스"
    )
    parts = PartIndex()
    assert requested_part_numbers(prompt, parts) == ["ESP32-C3", "BME280", "SHT30"]
    # shape alone is not enough — RS485 looks like a part number and is not one
    assert "RS485" in requested_part_numbers(prompt)


def test_short_exact_library_id_is_preserved_and_matched():
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    requested = requested_part_numbers(
        "부품은 Device:LED와 Switch:SW_Push로 선정했습니다.", parts
    )
    assert "Device:LED" in requested
    assert part_present("Device:LED", "Device:LED")
    assert not part_present("Device:LED", "Device:LED_Small")


def test_standard_package_token_is_not_a_requested_part_number():
    from circuitgen.partindex import PartIndex

    requested = requested_part_numbers(
        "AMS1117-3.3의 SOT-223 패키지를 사용합니다.", PartIndex()
    )
    assert "AMS1117-3" in requested
    assert "SOT-223" not in requested


def test_only_the_prompt_can_create_a_requirement():
    # measured: the prompt named no part, the 7B invented LM2596 as a spec
    # value, and that vetoed a cited LDO pattern the prompt fitted exactly
    assert requested_part_numbers("12V 입력에서 5V를 만드는 레귤레이터 회로") == []
    assert requested_part_numbers("STM32G474RET6 보드") == ["STM32G474RET6"]


def test_ordering_code_variants_satisfy_the_request():
    # KiCad names a whole ordering family with a trailing x
    assert part_present("STM32G474RET6", "MCU_ST_STM32G4:STM32G474RETx")
    assert part_present("Si7051", "Sensor_Temperature:Si7051-A20")
    assert not part_present("ESP32-C3", "MCU_ST_STM32G4:STM32G474RETx")
    assert not part_present("BME280", "Sensor_Temperature:Si7050-A20")


def test_conceptual_placeholder_does_not_satisfy_selected_part():
    # measured: Conceptual:NE555D counted as selected_parts_in_board while
    # Timer:NE555D was never bound
    assert not part_present("NE555D", "Conceptual:NE555D")
    assert part_present("NE555D", "Timer:NE555D")


def test_substituted_part_is_reported_missing_not_silently_accepted():
    ir = CircuitIR("sub")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    ir.add(Component("U2", "Sensor_Temperature:Si7050-A20", "Si7050"))
    issues, requested, satisfied, missing = check_requirements(
        ir, "ESP32-C3에 BME280 센서를 붙여줘"
    )
    assert sorted(missing) == ["BME280", "ESP32-C3"]
    assert satisfied == []
    assert {i.rule for i in issues} == {"requested_part_missing"}
    assert all(i.severity == "error" for i in issues)


def test_named_part_that_is_present_passes():
    ir = CircuitIR("ok")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    _issues, _req, satisfied, missing = check_requirements(ir, "STM32G474RET6 보드")
    assert satisfied == ["STM32G474RET6"] and missing == []


# ---- power integrity --------------------------------------------------------


def test_unpowered_supply_pins_are_invisible_to_erc_but_not_to_compliance():
    ir = mcu_board(None)
    assert [i for i in check_circuit(ir, SYMS) if i.severity == "error"] == []

    issues, checked = check_power_integrity(ir, SYMS, load_device_limits())
    assert {i.rule for i in issues} == {"power_pin_unpowered"}
    assert {i.path for i in issues} == {"U1.1", "U1.2"}
    assert checked == [STM32]


def test_supply_above_absolute_maximum_is_an_error():
    ir = mcu_board("+5V")
    assert [i for i in check_circuit(ir, SYMS) if i.severity == "error"] == []

    issues, _checked = check_power_integrity(ir, SYMS, load_device_limits())
    assert {i.rule for i in issues} == {"supply_over_absolute_maximum"}
    assert all("4.0 V" in i.message for i in issues)


def test_supply_inside_the_operating_range_is_clean():
    issues, checked = check_power_integrity(mcu_board("+3V3"), SYMS, load_device_limits())
    assert issues == [] and checked == [STM32]


def test_supply_pin_stranded_on_a_signal_net_is_an_error():
    ir = mcu_board("+3V3")
    # move VDD off the rail onto an ordinary signal net
    for net in ir.nets:
        net.nodes = [n for n in net.nodes if n != ("U1", "1")]
    ir.connect("SOME_SIGNAL", ("U1", "1"))
    ir.connect("SOME_SIGNAL", ("R1", "1"))
    issues, _checked = check_power_integrity(ir, SYMS, load_device_limits())
    assert [i.rule for i in issues] == ["power_pin_on_signal_net"]


def test_device_without_recorded_limits_still_gets_the_structural_check():
    ir = mcu_board("+5V")
    issues, checked = check_power_integrity(ir, SYMS, limits=[])
    # no datasheet entry -> no voltage verdict, and silence is not a pass
    assert issues == [] and checked == []


def test_every_device_limit_carries_a_datasheet_citation():
    data = json.loads(DEVICE_LIMITS_PATH.read_text(encoding="utf-8"))
    assert data["devices"], "the limits file must not be empty"
    for device in data["devices"]:
        source = device["source"]
        assert source["document"] and source["claims"]
        for claim in source["claims"]:
            assert claim["table"] and claim["text"]
            assert isinstance(claim["pdf_page_index"], int)


def test_tmp100_limits_are_on_the_cited_datasheet_page():
    from pathlib import Path

    pdf = Path(__file__).resolve().parent.parent / "data" / "datasheets" / "tmp100_SBOS231I.pdf"
    if not pdf.is_file():
        pytest.skip("tmp100_SBOS231I.pdf is not in this checkout")
    import fitz

    doc = fitz.open(pdf)
    page = doc[3].get_text()
    assert "Recommended Operating Conditions" in page
    assert "2.7" in page and "5.5" in page
    assert "Power supply, V+" in page and "7.5" in page
    pins = doc[2].get_text()
    assert "Supply voltage, 2.7 V to 5.5 V" in pins


def test_compliance_report_combines_both_checks():
    report = check_compliance(mcu_board("+5V"), SYMS, "BME280 보드")
    assert not report.ok
    assert report.missing_parts == ["BME280"]
    assert {i.rule for i in report.errors} == {
        "requested_part_missing", "supply_over_absolute_maximum"
    }
    assert report.as_dict()["ok"] is False


# ---- rails the parts actually need ------------------------------------------


def test_ground_only_requirement_gains_a_logic_rail_for_its_mcu():
    """Measured on the bench: the I2C pattern supplied its own STM32 while the
    extracted spec listed rails ``[GND]``, so every supply pass was skipped and
    the board shipped with all MCU supply pins no-connect at ERC 0."""
    spec = {"power": {"rails": [{"name": "GND", "voltage": "0V"}]}}
    ir = CircuitIR("x")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    notes = ensure_device_supply_rails(spec, ir, load_device_limits())
    assert [r["name"] for r in spec["power"]["rails"]] == ["GND", "+3V3"]
    assert any("operates at 1.71–3.6 V" in n for n in notes)


def test_five_volt_only_requirement_gains_a_rail_the_mcu_survives():
    """The CAN board's spec listed only +5V, so the logic rail resolved to +5V
    and VDD landed 1.0 V above the absolute maximum — ERC clean, part dead."""
    spec = {"power": {"rails": [
        {"name": "+5V", "voltage": "5V"}, {"name": "GND", "voltage": "0V"},
    ]}}
    ir = CircuitIR("x")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    ensure_device_supply_rails(spec, ir, load_device_limits())
    assert [r["name"] for r in spec["power"]["rails"]] == ["+5V", "GND", "+3V3"]


def test_a_usable_rail_is_left_alone():
    spec = {"power": {"rails": [
        {"name": "+3V3", "voltage": "3.3V"}, {"name": "GND", "voltage": "0V"},
    ]}}
    ir = CircuitIR("x")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    assert ensure_device_supply_rails(spec, ir, load_device_limits()) == []


def test_devices_without_recorded_limits_do_not_invent_rails():
    spec = {"power": {"rails": [{"name": "GND", "voltage": "0V"}]}}
    ir = CircuitIR("x")
    ir.add(Component("U1", "Some_Vendor:UNKNOWN123", "UNKNOWN123"))
    assert ensure_device_supply_rails(spec, ir, load_device_limits()) == []
    assert [r["name"] for r in spec["power"]["rails"]] == ["GND"]


# --- "is the role present" vs "is the role doing its job" ------------------


def _two_pin(lib, prefix):
    return SymbolDef(
        lib, "",
        [PinDef("1", "", PinType.PASSIVE, 0, 0, 0, 2.54),
         PinDef("2", "", PinType.PASSIVE, 0, 0, 0, 2.54)],
        reference_prefix=prefix,
    )


def test_a_present_role_that_conducts_nothing_is_not_a_fulfilled_role():
    """The reproduction that motivated this: driver_relay reported
    role_fulfilment 1.0, compliance ok and all three selected parts present on
    a board whose base resistor had both ends on the same rail. Presence is
    unchanged — it is still the floor — and the second number is what moves."""
    from circuitgen.compliance import role_fulfilment, role_jobs_done
    from circuitgen.topology import analyze_conduction

    symbols = {
        "Device:R": _two_pin("Device:R", "R"),
        "power:+5V": SymbolDef(
            "power:+5V", "",
            [PinDef("1", "+5V", PinType.PWROUT, 0, 0, 0, 2.54)],
            reference_prefix="#PWR", is_power=True,
        ),
    }
    ir = CircuitIR("base-resistor")
    ir.add(Component("R1", "Device:R", "1k"))
    ir.add(Component("R2", "Device:R", "1k"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.connect("+5V", ("R1", "1"), ("R2", "1"), ("#PWR01", "1"))
    ir.connect("NOWHERE", ("R1", "2"), ("R2", "2"))
    spec = {"parts_needed": [{"role": "base resistor", "search_query": "R"}]}
    # the candidate list the agent records for the role, as the bench passes it
    cands = {"base resistor": [{"lib_id": "Device:R"}]}

    total, present, missing, _short, unver = role_fulfilment(spec, ir, symbols, cands)
    assert (total, present, missing, unver) == (1, 1, [], [])

    dead = analyze_conduction(ir, symbols).dead
    judged, working, broken = role_jobs_done(spec, ir, symbols, cands, dead)
    assert (judged, working) == (1, 0)
    assert "base resistor" in broken[0] and "same potential" in broken[0]


def test_compliance_reports_a_dead_component_as_an_error():
    """Unlike the role paraphrase warnings, this is a fact about the finished
    board: the part is there and no current can flow through it."""
    def _rail(lib, value):
        return SymbolDef(
            lib, "", [PinDef("1", value, PinType.PWROUT, 0, 0, 0, 2.54)],
            reference_prefix="#PWR", is_power=True,
        )

    symbols = {
        "Device:R": _two_pin("Device:R", "R"),
        "Device:C": _two_pin("Device:C", "C"),
        "power:+5V": _rail("power:+5V", "+5V"),
        "power:GND": _rail("power:GND", "GND"),
    }
    # an RC low-pass off +5V: every part bridges two different potentials
    ir = CircuitIR("rc")
    ir.add(Component("C1", "Device:C", "100nF"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    ir.connect("+5V", ("R1", "1"), ("#PWR01", "1"))
    ir.connect("SIG", ("R1", "2"), ("C1", "1"))
    ir.connect("GND", ("C1", "2"), ("#PWR02", "1"))
    report = check_compliance(ir, symbols, prompt="", spec={})
    assert report.dead_components == {}, report.dead_components

    # now short each of them across a single net
    ir.nets = []
    ir.connect("GND", ("C1", "1"), ("C1", "2"), ("#PWR02", "1"))
    ir.connect("SIG", ("R1", "1"), ("R1", "2"))
    report = check_compliance(ir, symbols, prompt="", spec={})
    assert set(report.dead_components) == {"C1", "R1"}
    assert not report.ok
    assert {i.rule for i in report.errors} == {"component_does_no_work"}


def test_requested_package_mismatch_blocks_ordering():
    symbols = {"Device:D": _two_pin("Device:D", "D")}
    ir = CircuitIR("package")
    ir.add(Component("D1", "Device:D", "1N4148", "Diode_THT:D_DO-35"))
    ir.connect("A", ("D1", "1"), ("X1", "1"))
    ir.connect("B", ("D1", "2"), ("X2", "1"))
    spec = {"parts_needed": [{
        "reference": "D1", "role": "d1", "search_query": "1N4148",
        "package": "SOD-123", "quantity": 1,
    }]}
    report = check_compliance(ir, symbols, spec=spec, transcribed=True)
    issue = next(i for i in report.errors if i.rule == "requested_package_mismatch")
    assert "SOD-123" in issue.message and "D_DO-35" in issue.message


@pytest.mark.parametrize("requested,footprint", [
    ("HC-49/SD SMD", "Crystal:Crystal_SMD_0603-2Pin_6.0x3.5mm"),
    ("2x3 Pin Header", "Connector:Tag-Connect_TC2030-IDC-FP_2x03_P1.27mm_Vertical"),
])
def test_named_package_families_are_physical_constraints(requested, footprint):
    symbols = {"Device:X": _two_pin("Device:X", "X")}
    ir = CircuitIR("package_family")
    ir.add(Component("X1", "Device:X", "part", footprint))
    ir.connect("A", ("X1", "1"), ("P1", "1"))
    ir.connect("B", ("X1", "2"), ("P2", "1"))
    spec = {"parts_needed": [{
        "reference": "X1", "role": "x1", "package": requested,
    }]}

    report = check_compliance(ir, symbols, spec=spec, transcribed=True)

    assert any(i.rule == "requested_package_mismatch" for i in report.errors)


def test_connector_family_in_search_query_is_a_physical_constraint():
    from circuitgen.fp_checks import requested_footprint_constraints, requested_package_text

    requested = requested_package_text({
        "search_query": "1x2 header", "package": "", "value": "",
    })
    _tokens, pitches, families = requested_footprint_constraints(requested)
    assert families == ["PINHEADER"]
    assert pitches == [2.54]


def test_polarized_cap_without_case_does_not_keep_arbitrary_footprint():
    from circuitgen.fp_checks import assign_footprints

    class Parts:
        @staticmethod
        def has_footprints():
            return True

    sym = _two_pin("Device:C_Polarized", "C")
    ir = CircuitIR("bulk_cap")
    ir.add(Component("C1", "Device:C_Polarized", "250uF", "Capacitor_SMD:C_0805_2012Metric"))
    notes = assign_footprints(
        ir, {"Device:C_Polarized": sym}, Parts(),
        requested_packages={"C1": "SMD electrolytic capacitor 250uF"},
    )
    assert ir.components["C1"].footprint == ""
    assert any("no concrete case size" in note for note in notes)


def test_conceptual_placeholder_blocks_ordering():
    ir = CircuitIR("placeholder")
    ir.add(Component("U1", "Conceptual:ESP32_WROOM_32E", "ESP32-WROOM-32E"))
    ir.connect("GND", ("U1", "1"), ("J1", "1"))
    report = check_compliance(ir, {}, spec={}, transcribed=True)
    assert any(i.rule == "conceptual_part_unresolved" for i in report.errors)


def test_transcription_reports_provenance_backed_pin_name_conflict():
    symbols = {
        "Interface_USB:CH340K": SymbolDef(
            "Interface_USB:CH340K", "", [
                PinDef("4", "~{DTR}", PinType.OUTPUT, 0, 0, 0, 2.54),
            ],
        )
    }
    ir = CircuitIR("ch340")
    ir.add(Component("U1", "Interface_USB:CH340K", "CH340K", "Package_SO:SSOP-10"))
    ir.connect("V3", ("U1", "4"))
    spec = {
        "parts_needed": [{"reference": "U1", "role": "usb_uart"}],
        "netlist": [{"name": "V3", "nodes": [{
            "reference": "U1", "pin": "4", "pin_name": "V3",
        }]}],
    }

    report = check_compliance(ir, symbols, spec=spec, transcribed=True)

    assert any(
        issue.rule == "canonical_pin_binding_conflict" for issue in report.errors
    )
