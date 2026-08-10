"""How many sheets a board is worth, and where the boundaries fall.

The rule under test: a group earns its own sheet when it holds two or more
multi-pin devices; anything smaller belongs on the sheet of the component the
board is wired to. The version this replaces gave every generator group a
sheet and mapped group names through a table, which produced twelve
one-component sheets for a thirteen-part board.
"""

from circuitgen.hierarchy import hub_ref, partition_by_function, validate_partition
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def _sym(lib, prefix, pin_count):
    return SymbolDef(
        lib, "",
        [PinDef(str(n), "", PinType.PASSIVE, 0, 0, 0, 2.54) for n in range(1, pin_count + 1)],
        reference_prefix=prefix,
    )


SYMS = {
    "X:MCU": _sym("X:MCU", "U", 48),
    "X:DRV": _sym("X:DRV", "U", 24),
    "X:ENC": _sym("X:ENC", "U", 14),
    "X:REG": _sym("X:REG", "U", 3),
    "Device:C": _sym("Device:C", "C", 2),
    "Device:R": _sym("Device:R", "R", 2),
    "Device:Crystal": _sym("Device:Crystal", "Y", 2),
}


def test_a_single_device_peripheral_shares_the_hub_sheet():
    """One driver, one encoder, one transceiver — each was getting a page of
    its own. They belong next to the controller pins they connect to."""
    ir = CircuitIR("board")
    ir.add(Component("U1", "X:MCU", "MCU", group="MCU"))
    ir.add(Component("U2", "X:DRV", "DRV", group="MOTOR1"))
    ir.add(Component("C1", "Device:C", "100nF", group="MOTOR1"))
    ir.add(Component("U3", "X:ENC", "ENC", group="ENCODER1"))
    ir.connect("PWM_A1", ("U1", "1"), ("U2", "1"))
    ir.connect("DRV_LOCAL", ("U2", "2"), ("C1", "1"))
    ir.connect("SPI_SCK", ("U1", "2"), ("U3", "1"))

    assert hub_ref(ir, SYMS) == "U1"
    sheets = partition_by_function(ir, SYMS)
    assert set(sheets) == {"MCU"}
    assert sheets["MCU"].components == {"U1", "U2", "C1", "U3"}
    # everything is on one sheet, so nothing has to leave it
    assert sheets["MCU"].ports == set()
    assert {"PWM_A1", "DRV_LOCAL", "SPI_SCK"} <= sheets["MCU"].local_nets
    assert validate_partition(ir, sheets) == []


def test_a_substantial_subsystem_keeps_its_own_sheet():
    """The user's own example: a driver stage with more than one IC, its own
    supply conversion and its own crystal is read on its own page."""
    ir = CircuitIR("board")
    ir.add(Component("U1", "X:MCU", "MCU", group="MCU"))
    ir.add(Component("U2", "X:DRV", "HDMI_DRV", group="HDMI"))
    ir.add(Component("U3", "X:REG", "REG", group="HDMI"))
    ir.add(Component("Y1", "Device:Crystal", "27MHz", group="HDMI"))
    ir.add(Component("C1", "Device:C", "100nF", group="HDMI"))
    ir.connect("HDMI_CLK", ("U1", "1"), ("U2", "1"))
    ir.connect("HDMI_1V8", ("U3", "2"), ("U2", "2"), ("C1", "1"))
    ir.connect("XTAL", ("U2", "3"), ("Y1", "1"))
    # the controller is what the rest of the board hangs off — on the measured
    # board it carried 22 connections to the next component's 3
    for pin in range(2, 12):
        ir.connect(f"GPIO{pin}", ("U1", str(pin)))

    sheets = partition_by_function(ir, SYMS)
    assert set(sheets) == {"MCU", "HDMI"}
    assert sheets["HDMI"].components == {"U2", "U3", "Y1", "C1"}
    # the crystal and the local supply stay inside; only the MCU link leaves
    assert {"HDMI_1V8", "XTAL"} <= sheets["HDMI"].local_nets
    assert "HDMI_CLK" in sheets["HDMI"].ports
    assert "HDMI_CLK" in sheets["MCU"].ports
    assert validate_partition(ir, sheets) == []


def test_four_identical_single_device_channels_do_not_become_four_sheets():
    """Measured: a 13-part board came out as twelve child sheets, four of them
    holding one motor driver each."""
    ir = CircuitIR("motors")
    ir.add(Component("U1", "X:MCU", "MCU", group="MCU"))
    for channel in range(1, 5):
        ir.add(Component(f"U{channel + 1}", "X:DRV", "DRV", group=f"MOTOR{channel}"))
        ir.connect(f"PWM{channel}", ("U1", str(channel)), (f"U{channel + 1}", "1"))
    sheets = partition_by_function(ir, SYMS)
    assert set(sheets) == {"MCU"}
    assert validate_partition(ir, sheets) == []


def test_without_symbols_every_group_still_gets_a_home():
    """No symbol table means no device count; the partition must still be
    total and valid rather than dropping components."""
    ir = CircuitIR("bare")
    ir.add(Component("U1", "X:MCU", "MCU", group="MCU"))
    ir.add(Component("U2", "X:DRV", "DRV", group="MOTOR1"))
    ir.connect("PWM", ("U1", "1"), ("U2", "1"))
    sheets = partition_by_function(ir)
    assert sum(len(s.components) for s in sheets.values()) == 2
    assert validate_partition(ir, sheets) == []
