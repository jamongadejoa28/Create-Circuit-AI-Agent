"""The explanation the user gets, and where each sentence comes from.

Every statement must be computed from an artefact — the requirement, the block
plan, the finished IR, the symbols, the compliance report. A section with no
facts behind it must be absent rather than reassuring.
"""

from circuitgen.compliance import check_compliance
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType
from circuitgen.rationale import explain


def _sym(lib, prefix, pins, power=False):
    return SymbolDef(
        lib, "",
        [PinDef(n, name, et, 0, 0, 0, 2.54) for n, name, et in pins],
        reference_prefix=prefix, is_power=power,
    )


SYMS = {
    "MCU_ST_STM32G4:STM32G474CBTx": _sym(
        "MCU_ST_STM32G4:STM32G474CBTx", "U",
        [(str(n), f"PA{n}", PinType.BIDIR) for n in range(1, 40)]),
    "Driver_Motor:DRV8311H": _sym(
        "Driver_Motor:DRV8311H", "U",
        [(str(n), f"P{n}", PinType.BIDIR) for n in range(1, 25)]),
    "Conceptual:STS3215_UART": _sym(
        "Conceptual:STS3215_UART", "U",
        [("TXD", "TXD", PinType.PASSIVE), ("RXD", "RXD", PinType.PASSIVE)]),
    "Device:R": _sym("Device:R", "R",
                     [("1", "", PinType.PASSIVE), ("2", "", PinType.PASSIVE)]),
}


def _board():
    ir = CircuitIR("board")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474CBTx", "STM32G474"))
    ir.add(Component("U2", "Driver_Motor:DRV8311H", "DRV8311H"))
    ir.add(Component("U9", "Conceptual:STS3215_UART", "STS3215"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("PWM1", ("U1", "1"), ("U2", "15"))
    ir.connect("UART_TX", ("U1", "2"), ("U9", "TXD"))
    ir.connect("UART_RX", ("U9", "RXD"))
    return ir


PLAN = [
    {"id": "MCU", "count": 1, "interface_nets": [{"name": "UART_TX"}]},
    {"id": "MOTOR", "count": 4,
     "interface_nets": [{"name": "PWM{n}"}, {"name": "SCK"}, {"name": "MISO"}]},
]


def test_it_separates_the_parts_you_named_from_the_ones_it_chose():
    ir = _board()
    out = {x["title"]: x["detail"] for x in explain(
        "STM32G474 하나로 BLDC 모터를 제어", {}, PLAN, ir, SYMS, None, None
    )}
    # with no part index no token can be verified, so nothing is claimed named
    assert "지정하신 부품" not in out
    assert "U2 (Driver_Motor:DRV8311H)" in out["대신 고른 부품"]


def test_a_conceptual_box_is_reported_with_its_pins_not_as_an_empty_box():
    ir = _board()
    out = {x["title"]: x["detail"] for x in explain(
        "x", {}, PLAN, ir, SYMS, None, None
    )}
    detail = out["라이브러리에 없어 개념 심볼로 그린 것"]
    assert "U9" in detail and "RXD" in detail and "TXD" in detail
    assert "발주 전에" in detail  # it says what the user must still do


def test_the_pin_budget_is_stated_with_both_sides_of_the_arithmetic():
    ir = _board()
    out = {x["title"]: x["detail"] for x in explain(
        "x", {}, PLAN, ir, SYMS, None, None
    )}
    detail = out["컨트롤러 패키지"]
    assert "STM32G474CBTx" in detail and "39" in detail
    # 1 MCU interface + 4x3 motor interfaces
    assert "13" in detail


def test_shared_interfaces_of_a_repeated_block_are_named():
    ir = _board()
    out = {x["title"]: x["detail"] for x in explain(
        "x", {}, PLAN, ir, SYMS, None, None
    )}
    detail = out["여러 개가 공유하는 신호"]
    assert "MOTOR × 4" in detail and "SCK" in detail and "MISO" in detail
    assert "PWM{n}" not in detail  # already per-instance


def test_no_section_is_invented_when_there_is_nothing_to_say():
    ir = CircuitIR("empty")
    ir.add(Component("R1", "Device:R", "10k"))
    assert explain("x", {}, [], ir, SYMS, None, None) == []


def test_a_blocking_compliance_error_is_restated_as_do_not_order():
    ir = _board()
    report = check_compliance(ir, SYMS, prompt="", spec={})
    assert report.errors, "the fixture board has dead parts, that is the point"
    out = {x["title"]: x["detail"] for x in explain(
        "x", {}, PLAN, ir, SYMS, report, None
    )}
    assert "발주하시면 안 됩니다" in out["발주 전에 반드시 해결해야 하는 것"]
