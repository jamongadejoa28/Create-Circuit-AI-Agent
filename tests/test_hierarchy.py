from circuitgen.hierarchy import partition_by_function, validate_partition
from circuitgen.ir import CircuitIR, Component


def test_function_partition_keeps_local_nets_and_exposes_cross_sheet_ports():
    ir = CircuitIR("hierarchy")
    ir.add(Component("U1", "X:MCU", "MCU", group="MCU"))
    ir.add(Component("U2", "X:DRV", "DRV", group="BLDCMOTOR11"))
    ir.add(Component("C1", "Device:C", "100nF", group="BLDCMOTOR11"))
    ir.add(Component("U3", "X:ENC", "ENC", group="ENCODER1"))
    ir.connect("PWM_A1", ("U1", "1"), ("U2", "1"))
    ir.connect("DRV_LOCAL", ("U2", "2"), ("C1", "1"))
    ir.connect("SPI_SCK", ("U1", "2"), ("U3", "1"))
    sheets = partition_by_function(ir)
    assert set(sheets) == {"MCU_CAN_DEBUG", "MOTOR_1", "ENCODER_1"}
    assert "DRV_LOCAL" in sheets["MOTOR_1"].local_nets
    assert "PWM_A1" in sheets["MOTOR_1"].ports
    assert "PWM_A1" in sheets["MCU_CAN_DEBUG"].ports
    assert "SPI_SCK" in sheets["ENCODER_1"].ports
    assert validate_partition(ir, sheets) == []


def test_repeated_motor_groups_become_distinct_sheets():
    ir = CircuitIR("motors")
    for channel in range(1, 5):
        ir.add(Component(f"U{channel}", "X:DRV", "DRV", group=f"BLDCMOTOR1{channel}"))
    sheets = partition_by_function(ir)
    assert set(sheets) == {"MOTOR_1", "MOTOR_2", "MOTOR_3", "MOTOR_4"}
    assert validate_partition(ir, sheets) == []
