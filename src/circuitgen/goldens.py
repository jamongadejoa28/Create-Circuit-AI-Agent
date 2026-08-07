"""Golden circuits 2-5 (plan §10) — built against the real part index
and iterated to KiCad ERC 0 by fixture-builder agents; each carries
its discovery notes (hidden stacked pins, BOOT0/NRST pin sharing).
Golden 1 lives in examples.py.
"""

from .ir import CircuitIR, Component

def golden2_mcu_minimal_ir() -> CircuitIR:
    ir = CircuitIR(name="golden2_mcu_minimal")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx",
                     "Package_QFP:LQFP-64_10x10mm_P0.5mm"))
    ir.add(Component("J1", "Connector:Conn_ARM_JTAG_SWD_10", "SWD",
                     "Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical"))
    ir.add(Component("C1", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 16
    ir.add(Component("C2", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 32
    ir.add(Component("C3", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 48
    ir.add(Component("C4", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 64
    ir.add(Component("C5", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDDA
    ir.add(Component("C6", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric"))   # bulk
    ir.add(Component("C7", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # NRST
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))     # BOOT0 pull-down
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    ir.connect(
        "+3V3",
        ("U1", "1"),   # VBAT
        ("U1", "16"), ("U1", "32"), ("U1", "48"), ("U1", "64"),  # VDD
        ("U1", "29"),  # VDDA
        ("U1", "28"),  # VREF+
        ("C1", "1"), ("C2", "1"), ("C3", "1"), ("C4", "1"),
        ("C5", "1"), ("C6", "1"),
        ("J1", "1"),   # VTref
        ("#PWR01", "1"),
    )
    # all VSS pins including the hidden stacked ones (31/47/63) + VSSA
    ir.connect(
        "GND",
        ("U1", "15"), ("U1", "27"), ("U1", "31"), ("U1", "47"), ("U1", "63"),
        ("C1", "2"), ("C2", "2"), ("C3", "2"), ("C4", "2"),
        ("C5", "2"), ("C6", "2"), ("C7", "2"),
        ("R1", "2"),
        ("J1", "3"), ("J1", "5"), ("J1", "9"),  # GND, hidden stacked GND, GNDDetect
        ("#PWR02", "1"),
    )
    ir.connect("NRST", ("U1", "7"), ("C7", "1"), ("J1", "10"))   # PG10/NRST
    ir.connect("BOOT0", ("U1", "61"), ("R1", "1"))               # PB8-BOOT0
    ir.connect("SWDIO", ("U1", "49"), ("J1", "2"))               # PA13
    ir.connect("SWCLK", ("U1", "50"), ("J1", "4"))               # PA14

    # every remaining MCU pin is an explicit no-connect; J1 pins 6 (SWO)
    # and 8 (TDI) unused, pin 7 (KEY) is NC-typed in the library and
    # needs no marker
    used = {"1", "7", "15", "16", "27", "28", "29", "31", "32", "47", "48",
            "49", "50", "61", "63", "64"}
    ir.nc_pins = [("U1", str(n)) for n in range(1, 65) if str(n) not in used]
    ir.nc_pins += [("J1", "6"), ("J1", "8")]
    return ir


def golden3_mcu_i2c_ir() -> CircuitIR:
    """GOLDEN 3: STM32G474RETx + Si7050-A20 I2C temperature sensor.

    Minimal MCU hookup — all VDD/VBAT/VREF+/VDDA on +3V3 with one 100nF per
    VDD pin, a dedicated 100nF for VDDA and a 10uF bulk; all VSS/VSSA on GND;
    BOOT0 (PB8, pin 61 — shared with the BOOT0 strap on STM32G4) pulled down
    10k. I2C1 runs on PB9/pin 62 (SDA, AF4) and PA15/pin 51 (SCL, AF4) with
    10k pull-ups to +3V3; nets are named exactly SDA/SCL so the i2c_pullup
    lint is exercised. Sensor gets its own 100nF. Every unused MCU pin is an
    explicit no-connect (sensor pins 3/4 are NC-typed in the library and need
    no marker).
    """
    ir = CircuitIR(name="golden3_mcu_i2c")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx",
                     "Package_QFP:LQFP-64_10x10mm_P0.5mm"))
    ir.add(Component("U2", "Sensor_Temperature:Si7050-A20", "Si7050-A20",
                     "Package_DFN_QFN:DFN-6-1EP_3x3mm_P1mm_EP1.5x2.4mm"))
    # one 100nF per MCU VDD pin (16/32/48/64), one for VDDA, one bulk 10uF
    for ref in ("C1", "C2", "C3", "C4", "C5"):
        ir.add(Component(ref, "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("C6", "Device:C", "10uF", "Capacitor_SMD:C_0805_2012Metric"))
    ir.add(Component("C7", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # sensor
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # SDA pull-up
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # SCL pull-up
    ir.add(Component("R3", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # BOOT0 pull-down
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    ir.connect(
        "+3V3",
        ("U1", "1"),   # VBAT
        ("U1", "16"),  # VDD
        ("U1", "28"),  # VREF+
        ("U1", "29"),  # VDDA
        ("U1", "32"),  # VDD
        ("U1", "48"),  # VDD
        ("U1", "64"),  # VDD
        ("U2", "5"),   # sensor VDD
        ("C1", "1"), ("C2", "1"), ("C3", "1"), ("C4", "1"),
        ("C5", "1"), ("C6", "1"), ("C7", "1"),
        ("R1", "1"), ("R2", "1"),
        ("#PWR01", "1"),
    )
    ir.connect(
        "GND",
        ("U1", "15"), ("U1", "27"), ("U1", "31"), ("U1", "47"), ("U1", "63"),
        ("U2", "2"),
        ("C1", "2"), ("C2", "2"), ("C3", "2"), ("C4", "2"),
        ("C5", "2"), ("C6", "2"), ("C7", "2"),
        ("R3", "2"),
        ("#PWR02", "1"),
    )
    ir.connect("SDA", ("U1", "62"), ("U2", "1"), ("R1", "2"))  # PB9 = I2C1_SDA
    ir.connect("SCL", ("U1", "51"), ("U2", "6"), ("R2", "2"))  # PA15 = I2C1_SCL
    ir.connect("BOOT0", ("U1", "61"), ("R3", "1"))             # PB8/BOOT0 strap low

    used = {"1", "15", "16", "27", "28", "29", "31", "32", "47", "48",
            "51", "61", "62", "63", "64"}
    ir.nc_pins = [("U1", str(n)) for n in range(1, 65) if str(n) not in used]
    return ir


def golden4_mcu_spi_ir() -> CircuitIR:
    ir = CircuitIR(name="golden4_mcu_spi")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx",
                     "Package_QFP:LQFP-64_10x10mm_P0.5mm"))
    ir.add(Component("U2", "Memory_Flash:W25Q32JVSS", "W25Q32JVSS",
                     "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm"))
    ir.add(Component("C1", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 16
    ir.add(Component("C2", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 32
    ir.add(Component("C3", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 48
    ir.add(Component("C4", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDD pin 64
    ir.add(Component("C5", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDDA
    ir.add(Component("C6", "Device:C", "10uF", "Capacitor_SMD:C_0603_1608Metric"))  # bulk
    ir.add(Component("C7", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # flash VCC
    ir.add(Component("R1", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # /CS pull-up
    ir.add(Component("R2", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # /WP tie-high
    ir.add(Component("R3", "Device:R", "10k", "Resistor_SMD:R_0603_1608Metric"))  # /HOLD tie-high
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    # VBAT(1), VDD(16/32/48/64), VDDA(29), VREF+(28) all on +3V3; flash VCC(8).
    ir.connect(
        "+3V3",
        ("U1", "1"), ("U1", "16"), ("U1", "28"), ("U1", "29"),
        ("U1", "32"), ("U1", "48"), ("U1", "64"),
        ("U2", "8"),
        ("C1", "1"), ("C2", "1"), ("C3", "1"), ("C4", "1"),
        ("C5", "1"), ("C6", "1"), ("C7", "1"),
        ("R1", "1"), ("R2", "1"), ("R3", "1"),
        ("#PWR01", "1"),
    )
    # all VSS pins including the hidden stacked ones (31/47/63) plus VSSA(27)
    ir.connect(
        "GND",
        ("U1", "15"), ("U1", "27"), ("U1", "31"), ("U1", "47"), ("U1", "63"),
        ("U2", "4"),
        ("C1", "2"), ("C2", "2"), ("C3", "2"), ("C4", "2"),
        ("C5", "2"), ("C6", "2"), ("C7", "2"),
        ("#PWR02", "1"),
    )
    # SPI1 on PA5/PA6/PA7, chip-select on PA4
    ir.connect("SPI_SCK", ("U1", "19"), ("U2", "6"))   # PA5 -> CLK
    ir.connect("SPI_MISO", ("U1", "20"), ("U2", "2"))  # PA6 <- DO/IO1
    ir.connect("SPI_MOSI", ("U1", "21"), ("U2", "5"))  # PA7 -> DI/IO0
    ir.connect("SPI_CS", ("U1", "18"), ("U2", "1"), ("R1", "2"))  # PA4, 10k pull-up
    # /WP and /HOLD tied high through 10k per common practice
    ir.connect("FLASH_WP", ("U2", "3"), ("R2", "2"))
    ir.connect("FLASH_HOLD", ("U2", "7"), ("R3", "2"))

    # every remaining MCU pin is an explicit no-connect
    used = {"1", "15", "16", "18", "19", "20", "21", "27", "28", "29",
            "31", "32", "47", "48", "63", "64"}
    ir.nc_pins = [("U1", str(n)) for n in range(1, 65) if str(n) not in used]
    return ir


def golden5_mcu_uart_ir() -> CircuitIR:
    ir = CircuitIR(name="golden5_mcu_uart")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474RETx",
                     "Package_QFP:LQFP-64_10x10mm_P0.5mm"))
    # power input: regulated +3V3 arrives on a 2-pin screw terminal
    ir.add(Component("J1", "Connector:Screw_Terminal_01x02", "PWR_IN",
                     "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal"))
    # UART header: TX / RX / 3V3 / GND
    ir.add(Component("J2", "Connector:Conn_01x04_Pin", "UART",
                     "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"))
    # decoupling: one 100nF per VDD pin (16/32/48/64), one for VDDA, one 10uF bulk
    ir.add(Component("C1", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("C2", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("C3", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("C4", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))
    ir.add(Component("C5", "Device:C", "0.1uF", "Capacitor_SMD:C_0603_1608Metric"))  # VDDA
    ir.add(Component("C6", "Device:C", "10uF", "Capacitor_SMD:C_0603_1608Metric"))   # bulk
    # status LED driven by a GPIO through a 330R series resistor
    ir.add(Component("R1", "Device:R", "330R", "Resistor_SMD:R_0603_1608Metric"))
    ir.add(Component("D1", "Device:LED", "LED", "LED_SMD:LED_0603_1608Metric"))
    ir.add(Component("#PWR01", "power:+3V3", "+3V3"))
    ir.add(Component("#PWR02", "power:GND", "GND"))

    # VDD 16/32/48/64, VBAT 1, VDDA 29, VREF+ 28 all on the +3V3 rail
    ir.connect(
        "+3V3",
        ("U1", "1"), ("U1", "16"), ("U1", "28"), ("U1", "29"),
        ("U1", "32"), ("U1", "48"), ("U1", "64"),
        ("C1", "1"), ("C2", "1"), ("C3", "1"), ("C4", "1"), ("C5", "1"), ("C6", "1"),
        ("J1", "1"), ("J2", "3"),
        ("#PWR01", "1"),
    )
    # VSS 15 plus the hidden stacked VSS pins 31/47/63, VSSA 27
    ir.connect(
        "GND",
        ("U1", "15"), ("U1", "27"), ("U1", "31"), ("U1", "47"), ("U1", "63"),
        ("C1", "2"), ("C2", "2"), ("C3", "2"), ("C4", "2"), ("C5", "2"), ("C6", "2"),
        ("J1", "2"), ("J2", "4"), ("D1", "1"),
        ("#PWR02", "1"),
    )
    # USART1: PA9=TX (pin 43), PA10=RX (pin 44)
    ir.connect("UART_TX", ("U1", "43"), ("J2", "1"))
    ir.connect("UART_RX", ("U1", "44"), ("J2", "2"))
    # LED: GPIO PA5 (pin 19) -> 330R -> LED anode, cathode to GND
    ir.connect("LED_CTRL", ("U1", "19"), ("R1", "1"))
    ir.connect("LED_A", ("R1", "2"), ("D1", "2"))

    used = {"1", "15", "16", "19", "27", "28", "29", "31", "32",
            "43", "44", "47", "48", "63", "64"}
    ir.nc_pins = [("U1", str(n)) for n in range(1, 65) if str(n) not in used]
    return ir
