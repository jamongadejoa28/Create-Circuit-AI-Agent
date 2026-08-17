"""A peripheral function name resolved to a package pin, from the datasheet.

The model writes what an engineer says — USART1_TX — and the symbol has
numbers. Every board in this session produced tokens like that (MCU1.TX,
MOTOR1_SLEEP, U11.UART_RX); they became phantom pins that self-ERC reported
round after round and no pass could fix.
"""

import json

import pytest

from circuitgen.ir import CircuitIR, Component
from circuitgen.partindex import PartIndex
from circuitgen.pinfunctions import (
    DATA,
    device_for,
    pins_for_function,
    resolve_function_ending,
    resolve_function_pin,
)

pytestmark = pytest.mark.skipif(
    not DATA.exists(), reason="data/mcu_pin_functions.json not extracted"
)


def test_the_extracted_map_agrees_with_the_datasheet_package_columns():
    """PA9 is pin 43 on the LQFP64 and 31 on the LQFP48 — the numbers printed
    in the datasheet's own package columns. The map and the KiCad symbols were
    produced by different people from the same document; they must agree."""
    parts = PartIndex()
    for lib, expected in (
        ("MCU_ST_STM32G4:STM32G474RETx", "43"),   # LQFP64
        ("MCU_ST_STM32G4:STM32G474CBTx", "31"),   # LQFP48
    ):
        sym = parts.load_symbols([lib])[lib]
        got = resolve_function_pin(lib, sym, "USART1_TX")
        assert got is not None, lib
        assert got[0] == expected, (lib, got)
        assert "DS12288" in got[1], got[1]


def test_a_function_on_several_pins_prefers_one_that_is_free():
    """USART1_TX is PA9 or PB6. Taking a pin that is already wired would move
    an existing connection instead of making a new one."""
    parts = PartIndex()
    lib = "MCU_ST_STM32G4:STM32G474RETx"
    sym = parts.load_symbols([lib])[lib]
    # Table 12 lists USART1_TX on several ports; this package has PA9 and PB6
    ports = pins_for_function(lib, "USART1_TX")
    assert "PA9" in ports and "PB6" in ports
    assert resolve_function_pin(lib, sym, "USART1_TX")[0] == "43"          # PA9
    assert resolve_function_pin(lib, sym, "USART1_TX", {"43"})[0] == "59"  # PB6


def test_an_unrecorded_device_or_function_resolves_to_nothing():
    """Silence, not a guess: this is a lookup, and a miss must stay a miss."""
    parts = PartIndex()
    lib = "MCU_ST_STM32G4:STM32G474RETx"
    sym = parts.load_symbols([lib])[lib]
    assert resolve_function_pin(lib, sym, "NOT_A_REAL_FUNCTION") is None
    other = "Interface_CAN_LIN:TJA1051T"
    assert device_for(other) is None
    assert resolve_function_pin(other, parts.load_symbols([other])[other], "TXD") is None


def test_an_i2c_line_resolves_to_a_recorded_instance_not_a_gpio():
    parts = PartIndex()
    lib = "MCU_ST_STM32G4:STM32G474RETx"
    sym = parts.load_symbols([lib])[lib]
    sda = resolve_function_ending(lib, sym, "SDA")
    scl = resolve_function_ending(lib, sym, "SCL")
    assert sda is not None and scl is not None
    assert sda[0] == resolve_function_pin(lib, sym, "I2C1_SDA")[0]
    assert scl[0] == resolve_function_pin(lib, sym, "I2C1_SCL")[0]
    assert "DS12288" in sda[1] and "I2C1_SDA" in sda[1]
    assert resolve_function_ending(lib, sym, "SDA", {sda[0]})[0] != sda[0]
    assert resolve_function_ending(lib, sym, "NOT_A_BUS") is None


def test_pin_carries_function_ending_matches_the_recorded_port():
    from circuitgen.pinfunctions import pin_carries_function_ending, resolve_function_pin

    parts = PartIndex()
    lib = "MCU_ST_STM32G4:STM32G474RETx"
    sym = parts.load_symbols([lib])[lib]
    sda = resolve_function_pin(lib, sym, "I2C1_SDA")[0]
    assert pin_carries_function_ending(lib, sym, sda, "SDA")
    assert not pin_carries_function_ending(lib, sym, "2", "SDA")  # PC13
    assert pin_carries_function_ending(lib, sym, "19", "SCK")  # PA5 SPI1_SCK
    assert pin_carries_function_ending(lib, sym, "21", "MOSI")  # PA7
    assert pin_carries_function_ending(lib, sym, "20", "MISO")  # PA6
    assert not pin_carries_function_ending(
        "RF_Module:ESP32-WROOM-32",
        parts.load_symbols(["RF_Module:ESP32-WROOM-32"])["RF_Module:ESP32-WROOM-32"],
        "21",
        "SDA",
    )


def test_the_agent_rewrites_a_function_token_into_a_pin_number():
    from circuitgen.agent import Agent

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    ir = CircuitIR("names")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474"))
    ir.add(Component("U2", "Interface_CAN_LIN:TJA1051T", "TJA1051T"))
    ir.connect("CAN_TX", ("U1", "FDCAN1_TX"), ("U2", "1"))
    ir.connect("CAN_RX", ("U1", "FDCAN1_RX"), ("U2", "4"))

    notes = agent.resolve_pin_names(ir)
    nodes = {net.name: set(net.nodes) for net in ir.nets}
    assert ("U1", "46") in nodes["CAN_TX"], notes   # PA12
    assert ("U1", "45") in nodes["CAN_RX"], notes   # PA11
    assert all("DS12288" in n for n in notes if "FDCAN" in n), notes


def test_line_wrap_damage_is_recorded_and_not_repaired():
    """The PDF's own text layer drops a character at some wraps: PA9 reads
    OMP5_OUT where the page says COMP5_OUT. Repairing a token that is a strict
    suffix of another would be wrong once in three — TIM1_ETR is a real
    function and also a suffix of LPTIM1_ETR — so both are kept and the
    suspicion is written down."""
    raw = json.loads(DATA.read_text(encoding="utf-8"))["devices"][0]
    suspect = raw["suspect_wrap"]
    assert "OMP5_OUT" in suspect and "COMP5_OUT" in suspect["OMP5_OUT"]
    assert "TIM1_ETR" in suspect, "the false positive must be visible too"
    # kept, not deleted: a lookup for the real name still works
    assert pins_for_function(raw["match"], "COMP5_OUT")
    assert pins_for_function(raw["match"], "TIM1_ETR")
