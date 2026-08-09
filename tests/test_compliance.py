"""Requirement compliance + power integrity — the two questions ERC cannot answer.

Both rules exist because a board shipped as "ok" without them: an MCU whose
supply pins were all no-connect, and an STM32G474 with VDD on +5V, each at
KiCad ERC 0.
"""

import json

from circuitgen.compliance import (
    DEVICE_LIMITS_PATH,
    check_compliance,
    check_power_integrity,
    check_requirements,
    load_device_limits,
    part_present,
    requested_part_numbers,
)
from circuitgen.erc import check_circuit
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.patterns import (
    load_patterns,
    out_of_scope_subsystems,
    requested_subsystems,
)
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
    prompt = (
        "ESP32-C3에 BME280과 SHT30을 연결. RS485(Modbus RTU), CAN-FD, 24V, "
        "0805 저항, PA15/PB9 핀, I2C1 버스, IP65 케이스"
    )
    assert requested_part_numbers(prompt) == ["ESP32-C3", "BME280", "SHT30"]


def test_part_numbers_come_from_the_spec_too():
    spec = {"parts_needed": [
        {"role": "mcu", "search_query": "STM32G474RET6"},
        {"role": "r", "search_query": "resistor", "value": "10k"},
    ]}
    assert requested_part_numbers("MCU 보드", spec) == ["STM32G474RET6"]


def test_ordering_code_variants_satisfy_the_request():
    # KiCad names a whole ordering family with a trailing x
    assert part_present("STM32G474RET6", "MCU_ST_STM32G4:STM32G474RETx")
    assert part_present("Si7051", "Sensor_Temperature:Si7051-A20")
    assert not part_present("ESP32-C3", "MCU_ST_STM32G4:STM32G474RETx")
    assert not part_present("BME280", "Sensor_Temperature:Si7050-A20")


def test_substituted_part_is_reported_missing_not_silently_accepted():
    ir = CircuitIR("sub")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    ir.add(Component("U2", "Sensor_Temperature:Si7050-A20", "Si7050"))
    spec = {"parts_needed": [{"role": "sensor", "search_query": "BME280"}]}
    issues, requested, satisfied, missing = check_requirements(
        spec, ir, "ESP32-C3에 BME280 센서를 붙여줘"
    )
    assert sorted(missing) == ["BME280", "ESP32-C3"]
    assert satisfied == []
    assert {i.rule for i in issues} == {"requested_part_missing"}
    assert all(i.severity == "error" for i in issues)


def test_named_part_that_is_present_passes():
    ir = CircuitIR("ok")
    ir.add(Component("U1", STM32, "STM32G474RETx"))
    _issues, _req, satisfied, missing = check_requirements({}, ir, "STM32G474RET6 보드")
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


def test_compliance_report_combines_both_checks():
    report = check_compliance(
        {"parts_needed": [{"role": "sensor", "search_query": "BME280"}]},
        mcu_board("+5V"),
        SYMS,
        "BME280 보드",
    )
    assert not report.ok
    assert report.missing_parts == ["BME280"]
    assert {i.rule for i in report.errors} == {
        "requested_part_missing", "supply_over_absolute_maximum"
    }
    assert report.as_dict()["ok"] is False


# ---- pattern scope ----------------------------------------------------------


def test_multi_subsystem_board_is_not_answered_by_a_single_function_pattern():
    patterns = load_patterns()
    plc = (
        "산업용 24V PLC 컨트롤러. STM32를 메인 MCU로 사용하며 Digital Input 16채널, "
        "Relay Output 8채널, RS485, Ethernet을 포함한 회로도를 설계해주세요."
    )
    assert requested_subsystems(plc) >= {"relay", "rs485", "ethernet", "digital_io"}
    uncovered = out_of_scope_subsystems(plc, patterns["relay_driver"])
    assert uncovered == {"rs485", "ethernet", "digital_io"}


def test_single_function_request_still_reaches_its_pattern():
    patterns = load_patterns()
    relay = "3.3V GPIO로 12V 릴레이를 구동하는 회로를 만들어줘. 트랜지스터와 플라이백 다이오드 포함"
    assert out_of_scope_subsystems(relay, patterns["relay_driver"]) == set()
    i2c = "MCU에 I2C 온도센서를 연결해줘. 풀업과 디커플링 포함"
    assert out_of_scope_subsystems(i2c, patterns["i2c_temperature_sensor"]) == set()


def test_subsystem_keywords_need_word_boundaries():
    # "can" inside ordinary English, "ble" inside "assemble"
    assert requested_subsystems("this can be assembled by hand") == set()
    assert "can" in requested_subsystems("CAN-FD 통신 인터페이스")
