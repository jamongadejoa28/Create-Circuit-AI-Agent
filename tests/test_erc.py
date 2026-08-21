"""Unit tests for the self-hosted ERC (SKIDL-ported rules)."""

from circuitgen.erc import check_circuit
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.normalize import ensure_pwr_flags
from circuitgen.pins import PinType


def sym(lib_id, pin_specs, is_power=False, ref="U"):
    return SymbolDef(
        lib_id=lib_id,
        raw_sexp=f'(symbol "{lib_id.split(":")[1]}")',
        pins=[
            PinDef(number=n, name=(name if isinstance(name, str) else n), etype=t, x=0, y=0, orientation=0, length=1.27)
            for n, name, t in (
                (s[0], s[1] if len(s) == 3 else s[0], s[-1]) for s in pin_specs
            )
        ],
        is_power=is_power,
        reference_prefix=ref,
    )


SYMS = {
    "test:OUT2": sym("test:OUT2", [("1", PinType.OUTPUT), ("2", PinType.PASSIVE)]),
    "test:R": sym("test:R", [("1", PinType.PASSIVE), ("2", PinType.PASSIVE)], ref="R"),
    "test:C": sym("test:C", [("1", PinType.PASSIVE), ("2", PinType.PASSIVE)], ref="C"),
    "test:NC1": sym("test:NC1", [("1", PinType.NOCONNECT)]),
    "test:IN1": sym("test:IN1", [("1", PinType.INPUT)]),
    "test:MCU": sym(
        "test:MCU",
        [("1", "VCC", PinType.PWRIN), ("2", "GND", PinType.PWRIN),
         ("3", "SDA", PinType.BIDIR), ("4", "SCL", PinType.BIDIR)],
    ),
    "power:VCC": sym("power:VCC", [("1", PinType.PWRIN)], is_power=True, ref="#PWR"),
    "power:GND": sym("power:GND", [("1", PinType.PWRIN)], is_power=True, ref="#PWR"),
    "power:PWR_FLAG": sym("power:PWR_FLAG", [("1", PinType.PWROUT)], is_power=True, ref="#FLG"),
}


def mk(ir_components):
    ir = CircuitIR("t")
    for ref, lib in ir_components:
        ir.add(Component(ref, lib, lib.split(":")[1], footprint="F:F"))
    return ir


def rules(issues):
    return {(i.rule, i.severity) for i in issues}


def test_clean_two_passive_net():
    ir = mk([("R1", "test:R"), ("R2", "test:R")])
    ir.connect("A", ("R1", "1"), ("R2", "1"))
    ir.connect("B", ("R1", "2"), ("R2", "2"))
    issues = check_circuit(ir, SYMS)
    # PASSIVE drive exceeds NONE, so even a passive-only net has "a driver"
    # in SKiDL's model — a clean R-R circuit reports nothing at all.
    assert issues == []


def test_output_output_conflict_is_error():
    ir = mk([("U1", "test:OUT2"), ("U2", "test:OUT2")])
    ir.connect("N", ("U1", "1"), ("U2", "1"))
    ir.connect("M", ("U1", "2"), ("U2", "2"))
    issues = check_circuit(ir, SYMS)
    assert ("pin_conflict", "error") in rules(issues)


def test_unconnected_pin_is_error_and_nc_marker_accepted():
    ir = mk([("R1", "test:R"), ("R2", "test:R")])
    ir.connect("A", ("R1", "1"), ("R2", "1"))
    issues = check_circuit(ir, SYMS)
    unconnected = [i for i in issues if i.rule == "unconnected_pin"]
    # error severity — matches KiCad's pin_not_connected default
    assert {(i.path, i.severity) for i in unconnected} == {("R1.2", "error"), ("R2.2", "error")}

    ir.nc_pins = [("R1", "2"), ("R2", "2")]
    issues = check_circuit(ir, SYMS)
    assert not [i for i in issues if i.rule == "unconnected_pin"]


def test_nc_typed_pin_connected_is_error():
    ir = mk([("U1", "test:NC1"), ("R1", "test:R")])
    ir.connect("N", ("U1", "1"), ("R1", "1"))
    ir.nc_pins = [("R1", "2")]
    issues = check_circuit(ir, SYMS)
    assert ("nc_pin_connected", "error") in rules(issues)
    # conflict matrix also fires: NOCONNECT × PASSIVE = ERROR
    assert ("pin_conflict", "error") in rules(issues)


def test_unknown_symbol_pin_and_duplicate_membership():
    ir = mk([("U9", "test:MISSING"), ("R1", "test:R")])
    ir.connect("N", ("U9", "1"), ("R1", "7"), ("R1", "1"))
    ir.connect("M", ("R1", "1"), ("X1", "1"))
    got = rules(check_circuit(ir, SYMS))
    assert ("unknown_symbol", "error") in got
    assert ("unknown_pin", "error") in got
    assert ("unknown_component", "error") in got
    assert ("pin_multiple_nets", "error") in got


def test_power_net_needs_pwr_flag_for_drive():
    ir = mk([("U1", "test:IN1"), ("#PWR01", "power:VCC")])
    ir.connect("VCC", ("U1", "1"), ("#PWR01", "1"))
    issues = check_circuit(ir, SYMS)
    # PWRIN needs POWER drive; without PWR_FLAG the net max drive is NONE
    assert ("insufficient_drive", "warning") in rules(issues)

    added = ensure_pwr_flags(ir, SYMS)
    assert added == ["#FLG01"]
    issues = check_circuit(ir, SYMS)
    assert ("insufficient_drive", "warning") not in rules(issues)


def test_pwr_flag_not_duplicated():
    ir = mk([("U1", "test:IN1"), ("#PWR01", "power:VCC")])
    ir.connect("VCC", ("U1", "1"), ("#PWR01", "1"))
    assert ensure_pwr_flags(ir, SYMS) == ["#FLG01"]
    assert ensure_pwr_flags(ir, SYMS) == []  # second run adds nothing


def test_pwr_flag_is_not_added_to_a_signal_net_that_has_a_stray_supply_pin():
    """A PWRIN on SCL is a wiring error, not a power net to silence with a flag."""
    ir = mk([("U1", "test:MCU")])
    ir.connect("SCL", ("U1", "1"), ("U1", "4"))  # VCC + SCL on the same signal
    assert ensure_pwr_flags(ir, SYMS) == []
    assert not any(c.lib_id == "power:PWR_FLAG" for c in ir.components.values())


def test_stale_pwr_flag_on_a_signal_net_is_removed():
    ir = mk([("U1", "test:MCU"), ("#FLG01", "power:PWR_FLAG")])
    ir.connect("SCL", ("U1", "1"), ("U1", "4"), ("#FLG01", "1"))
    notes = ensure_pwr_flags(ir, SYMS)
    assert "removed:#FLG01" in notes
    assert "#FLG01" not in ir.components
    assert not any(("U1", "1") in n.nodes and n.name != "SCL" for n in ir.nets)
    assert ensure_pwr_flags(ir, SYMS) == []


# ---- extended rules ----


def _mcu_base():
    """MCU wired to VCC/GND rails with PWR_FLAGs; SDA/SCL still open."""
    ir = mk([
        ("U1", "test:MCU"),
        ("#PWR01", "power:VCC"),
        ("#PWR02", "power:GND"),
    ])
    ir.components["#PWR01"].value = "VCC"
    ir.components["#PWR02"].value = "GND"
    ir.connect("VCC", ("U1", "1"), ("#PWR01", "1"))
    ir.connect("GND", ("U1", "2"), ("#PWR02", "1"))
    ensure_pwr_flags(ir, SYMS)
    return ir


def test_decoupling_missing_then_satisfied():
    ir = _mcu_base()
    ir.connect("SDA", ("U1", "3"))
    ir.connect("SCL", ("U1", "4"))
    issues = check_circuit(ir, SYMS)
    assert ("decoupling_missing", "warning") in rules(issues)

    ir.add(Component("C1", "test:C", "0.1uF", footprint="F:F"))
    ir.connect("VCC", ("C1", "1"))
    ir.connect("GND", ("C1", "2"))
    issues = check_circuit(ir, SYMS)
    assert ("decoupling_missing", "warning") not in rules(issues)


def test_i2c_pullup_missing_then_satisfied():
    ir = _mcu_base()
    ir.add(Component("C1", "test:C", "0.1uF", footprint="F:F"))
    ir.connect("VCC", ("C1", "1"))
    ir.connect("GND", ("C1", "2"))
    ir.connect("SDA", ("U1", "3"))
    ir.connect("SCL", ("U1", "4"))
    issues = check_circuit(ir, SYMS)
    i2c = [i for i in issues if i.rule == "i2c_pullup_missing"]
    assert {i.path for i in i2c} == {"net:SDA", "net:SCL"}

    for i, net in enumerate(("SDA", "SCL"), start=1):
        ir.add(Component(f"R{i}", "test:R", "10k", footprint="F:F"))
        ir.connect(net, (f"R{i}", "1"))
        ir.connect("VCC", (f"R{i}", "2"))
    issues = check_circuit(ir, SYMS)
    assert ("i2c_pullup_missing", "warning") not in rules(issues)


def test_capacitor_across_sda_and_scl_is_a_warning():
    ir = _mcu_base()
    ir.add(Component("C1", "test:C", "0.01uF", footprint="F:F"))
    ir.connect("SDA", ("U1", "3"), ("C1", "1"))
    ir.connect("SCL", ("U1", "4"), ("C1", "2"))
    issues = check_circuit(ir, SYMS)
    cap = [i for i in issues if i.rule == "capacitor_across_i2c"]
    assert [i.path for i in cap] == ["C1"]


def test_a_capacitor_on_nets_named_sda_scl_without_sda_pins_is_not_a_bus_bypass():
    """Pull-ups may still treat the labels as I2C; the bypass checker must not.

    Electrically this is a 555 timing C. The nets are called SDA/SCL and
    no member pin is named SDA/SCL and no AF table records them.
    """
    from circuitgen.erc import capacitors_across_i2c_lines, is_i2c_net

    symbols = {
        **SYMS,
        "test:555": sym(
            "test:555",
            [
                ("1", "GND", PinType.PWRIN),
                ("6", "THRES", PinType.INPUT),
                ("7", "DISCH", PinType.OUTPUT),
                ("8", "VCC", PinType.PWRIN),
            ],
        ),
    }
    ir = mk([("U1", "test:555"), ("C1", "test:C")])
    ir.connect("SDA", ("U1", "7"), ("C1", "1"))
    ir.connect("SCL", ("U1", "6"), ("C1", "2"))
    assert capacitors_across_i2c_lines(ir, symbols) == []
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert is_i2c_net(ir, symbols, sda) and is_i2c_net(ir, symbols, scl)
    assert not any(i.rule == "capacitor_across_i2c" for i in check_circuit(ir, symbols))


def test_power_rails_shorted():
    ir = mk([("#PWR01", "power:VCC"), ("#PWR02", "power:GND"), ("R1", "test:R")])
    ir.components["#PWR01"].value = "VCC"
    ir.components["#PWR02"].value = "GND"
    ir.connect("OOPS", ("#PWR01", "1"), ("#PWR02", "1"), ("R1", "1"))
    ir.nc_pins = [("R1", "2")]
    ensure_pwr_flags(ir, SYMS)
    assert ("power_rails_shorted", "error") in rules(check_circuit(ir, SYMS))


def test_footprint_missing_warns_for_real_parts_only():
    ir = mk([("#PWR01", "power:VCC")])
    ir.add(Component("R1", "test:R", "1k"))  # no footprint
    ir.connect("VCC", ("R1", "1"), ("#PWR01", "1"))
    ir.nc_pins = [("R1", "2")]
    ensure_pwr_flags(ir, SYMS)
    fp = [i for i in check_circuit(ir, SYMS) if i.rule == "footprint_missing"]
    assert [i.path for i in fp] == ["R1"]


def test_spi_flash_cs_pullup_missing():
    from circuitgen.symbols import load_symbols
    from tests.test_blocks import _w25q_pin

    flash = "Memory_Flash:W25Q32JVSS"
    symbols = load_symbols([flash, "power:+3V3", "power:GND"])
    ir = CircuitIR("cs-erc")
    ir.add(Component("U2", flash, "W25Q32JVSS", footprint="F:F"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))
    cs = _w25q_pin(symbols, "CS")
    ir.connect("+3V3", ("#PWR01", "1"), ("U2", "8"))
    ir.connect("GND", ("#PWR02", "1"), ("U2", "4"))
    ir.connect("CS", ("U2", cs))
    ensure_pwr_flags(ir, symbols)

    issues = check_circuit(ir, symbols)
    assert any(i.rule == "spi_flash_cs_pullup_missing" for i in issues)
