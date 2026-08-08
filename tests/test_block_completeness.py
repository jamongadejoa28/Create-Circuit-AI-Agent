from circuitgen.blocks import validate_block_template
from circuitgen.ir import CircuitIR, Component


def test_required_catalog_role_cannot_disappear_even_if_erc_could_be_clean():
    block = {"id": "CONTROL", "roles": ["controller"]}
    candidates = {"controller": [{"lib_id": "MCU:Example"}]}
    ir = CircuitIR("missing_controller")
    ir.add(Component("C1", "Device:C", "100nF"))
    assert validate_block_template(block, ir, candidates) == [
        "block CONTROL: required role 'controller' has no catalog device"
    ]


def test_uncatalogued_role_accepts_explicit_conceptual_box():
    block = {"id": "MODULE", "roles": ["custom_module"]}
    ir = CircuitIR("concept")
    ir.add(Component("U1", "Conceptual:CustomModule", "CustomModule"))
    assert validate_block_template(block, ir, {"custom_module": []}) == []


def test_catalog_role_is_device_identity_not_a_project_specific_name():
    block = {"id": "SENSE", "roles": ["sensor"]}
    ir = CircuitIR("sense")
    ir.add(Component("U1", "Sensor:AnyCatalogPart", "sensor"))
    candidates = {"sensor": [{"lib_id": "Sensor:AnyCatalogPart"}]}
    assert validate_block_template(block, ir, candidates) == []
