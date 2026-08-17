"""Block decomposition: deterministic instantiation/merge and the mock
agent flow through plan → per-block synthesis → merge → pipeline."""

from pathlib import Path

import pytest

from circuitgen.blocks import instantiate_blocks, validate_plan
from circuitgen.ir import CircuitIR, Component
from circuitgen.ir_json import ir_from_json


def _led_block_ir():
    return ir_from_json({
        "name": "b",
        "components": [
            {"ref": "R1", "lib_id": "Device:R", "value": "330R", "footprint": "Resistor_SMD:R_0805_2012Metric"},
            {"ref": "D1", "lib_id": "Device:LED", "value": "LED", "footprint": "LED_SMD:LED_0805_2012Metric"},
        ],
        "nets": [
            {"name": "CTRL{n}", "nodes": [{"ref": "R1", "pin": "1"}]},   # interface
            {"name": "MID", "nodes": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "2"}]},  # local
            {"name": "GND", "nodes": [{"ref": "D1", "pin": "1"}]},        # rail
        ],
        "nc_pins": [],
    })


PLAN = [
    {"id": "LEDBLK", "description": "indicator", "roles": ["led"], "count": 3,
     "interface_nets": [{"name": "CTRL{n}", "purpose": "drive input"}]},
]


def test_instantiate_repeated_block():
    ir, notes = instantiate_blocks("t", PLAN, {"LEDBLK": _led_block_ir()}, rails=["+5V", "GND"])
    # 3 instances × 2 components, globally renumbered
    assert sorted(ir.components) == ["D1", "D2", "D3", "R1", "R2", "R3"]
    names = {n.name for n in ir.nets}
    # interface nets instance-stamped, locals namespaced, rail shared once
    assert {"CTRL1", "CTRL2", "CTRL3"} <= names
    assert {"LEDBLK1_MID", "LEDBLK2_MID", "LEDBLK3_MID"} <= names
    gnd = [n for n in ir.nets if n.name == "GND"]
    assert len(gnd) == 1 and len(gnd[0].nodes) == 3  # all cathodes on one rail net
    assert {c.group for c in ir.components.values()} == {"LEDBLK1", "LEDBLK2", "LEDBLK3"}


def test_single_instance_block_namespacing():
    plan = [{"id": "PWR", "description": "supply", "roles": [], "count": 1,
             "interface_nets": [{"name": "VOUT", "purpose": "regulated"}]}]
    blk = ir_from_json({
        "name": "b",
        "components": [{"ref": "C1", "lib_id": "Device:C", "value": "10uF"}],
        "nets": [
            {"name": "VOUT", "nodes": [{"ref": "C1", "pin": "1"}]},
            {"name": "FB", "nodes": [{"ref": "C1", "pin": "2"}]},
        ],
    })
    ir, _ = instantiate_blocks("t", plan, {"PWR": blk}, rails=["GND"])
    names = {n.name for n in ir.nets}
    assert "VOUT" in names          # interface keeps global name
    assert "PWR_FB" in names        # local gets block prefix


def test_validate_plan_orphans_and_dup_ids():
    spec = {"parts_needed": [{"role": "a"}, {"role": "b"}, {"role": "c"}]}
    plan = [
        {"id": "X", "roles": ["a"], "count": 1, "interface_nets": []},
        {"id": "X", "roles": ["b", "ghost"], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert fixed[1]["id"] == "XX"
    assert any(b["roles"] == ["c"] for b in fixed)  # orphan restored independently
    assert any("restored omitted role c" in n for n in notes)
    assert "ghost" not in fixed[1]["roles"]  # unknown role dropped
    assert notes


def test_validate_plan_corrects_repeated_hardware_count_from_requirements():
    spec = {
        "parts_needed": [
            {"role": "encoder", "search_query": "AS5048A", "quantity": 4},
            {"role": "driver", "search_query": "BLDC driver", "quantity": 4},
        ]
    }
    plan = [
        {"id": "ENC", "roles": ["encoder"], "count": 1, "interface_nets": []},
        {"id": "DRV", "roles": ["driver"], "count": 4, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert fixed[0]["count"] == 4
    assert fixed[1]["count"] == 4
    assert any("ENC" in n and "corrected" in n for n in notes)


def test_validate_plan_reports_shared_interfaces_instead_of_renaming_them():
    """It used to append {n} to every interface net outside a six-name list of
    bus names. On a real board the model wrote SCK / MISO / MOSI without the
    "SPI_" prefix that list expected, so all three were made per-instance and
    the board came out with FOUR SPI buses instead of one bus and four chip
    selects. Nothing available at plan time separates a clock from a select —
    both are INPUTs on the peripheral — so the plan is left as written and the
    sharing is reported to whoever reads the run.
    """
    spec = {"parts_needed": [{"role": "encoder", "quantity": 4}]}
    plan = [{
        "id": "ENC", "roles": ["encoder"], "count": 4,
        "interface_nets": [
            {"name": "SCK", "purpose": "shared clock"},
            {"name": "MISO", "purpose": "shared data"},
            {"name": "ENC_CS{n}", "purpose": "individual select"},
        ],
    }]
    fixed, notes = validate_plan(plan, spec)
    assert [n["name"] for n in fixed[0]["interface_nets"]] == [
        "SCK", "MISO", "ENC_CS{n}"
    ]
    shared_note = next(n for n in notes if n.startswith("block ENC:"))
    assert "SCK" in shared_note and "MISO" in shared_note
    assert "ENC_CS{n}" not in shared_note  # already per-instance
    assert "4 instances share" in shared_note


def test_validate_plan_removes_passive_only_decoupling_block():
    spec = {"parts_needed": [
        {"role": "driver", "quantity": 4},
        {"role": "Decoupling Capacitor", "quantity": 4},
    ]}
    plan = [
        {"id": "MOTOR", "roles": ["driver"], "count": 4, "interface_nets": []},
        {"id": "DECOUPLING", "roles": ["Decoupling Capacitor"], "count": 4, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert [b["id"] for b in fixed] == ["MOTOR"]
    assert any("passive-only" in n for n in notes)


def test_validate_plan_assigns_repeated_peripheral_role_to_one_owner():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "encoder", "search_query": "SPI encoder", "quantity": 4},
        {"role": "can_interface", "search_query": "CAN transceiver", "quantity": 1},
    ]}
    plan = [
        {"id": "MCU", "description": "main controller with encoders and CAN", "roles": ["controller", "encoder", "can_interface"], "count": 1, "interface_nets": []},
        {"id": "ENCODER", "description": "SPI encoder interface", "roles": ["encoder"], "count": 4, "interface_nets": []},
        {"id": "CAN", "description": "CAN communication", "roles": ["can_interface"], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    by_id = {b["id"]: b for b in fixed}
    assert by_id["MCU"]["roles"] == ["controller"]
    assert by_id["MCU"]["count"] == 1
    assert by_id["ENCODER"]["roles"] == ["encoder"]
    assert by_id["CAN"]["roles"] == ["can_interface"]
    assert sum("encoder" in b["roles"] for b in fixed) == 1
    assert any("duplicate ownership" in note for note in notes)


def test_validate_plan_splits_repeated_encoder_out_of_singleton_mcu_block():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "encoder", "search_query": "SPI encoder", "quantity": 4},
        {"role": "can_interface", "search_query": "CAN transceiver", "quantity": 1},
    ]}
    plan = [{
        "id": "MCU", "description": "controller with encoders and CAN",
        "roles": ["controller", "encoder", "can_interface"], "count": 4,
        "interface_nets": [
            {"name": "SPI_SCK", "purpose": "SPI encoder clock"},
            {"name": "SPI_CS{n}", "purpose": "encoder select"},
            {"name": "CAN_TX", "purpose": "CAN transmit"},
        ],
    }]
    fixed, notes = validate_plan(plan, spec)
    mcu = next(b for b in fixed if b["id"] == "MCU")
    enc = next(b for b in fixed if b["roles"] == ["encoder"])
    assert mcu["roles"] == ["controller", "can_interface"] and mcu["count"] == 1
    assert enc["count"] == 4
    assert {n["name"] for n in enc["interface_nets"]} == {"SPI_SCK", "SPI_CS{n}"}
    assert any("split from mixed block" in note for note in notes)


def test_validate_plan_removes_redundant_empty_motor_blocks():
    spec = {"parts_needed": [{"role": "driver", "search_query": "BLDC motor driver", "quantity": 4}]}
    plan = [
        {"id": "MOTOR1", "roles": ["driver"], "count": 4, "interface_nets": []},
        {"id": "MOTOR2", "roles": [], "count": 1, "interface_nets": []},
        {"id": "MOTOR3", "roles": [], "count": 1, "interface_nets": []},
    ]
    fixed, notes = validate_plan(plan, spec)
    assert [b["id"] for b in fixed] == ["MOTOR1"]
    assert any("empty-role" in note for note in notes)


def test_validate_plan_restores_power_requirements_and_reset_without_repeating_mcu():
    spec = {"parts_needed": [
        {"role": "controller", "search_query": "STM32G474", "quantity": 1},
        {"role": "reset_button", "search_query": "push button", "quantity": 1},
        {"role": "power_supply", "search_query": "power supply", "quantity": 1},
        {"role": "bulk_capacitor", "search_query": "bulk capacitor", "quantity": 1},
        {"role": "fuse", "search_query": "fuse", "quantity": 1},
        {"role": "tvss_diode", "search_query": "TVS diode", "quantity": 1},
    ]}
    plan = [{
        "id": "MCU", "description": "controller", "roles": ["controller"],
        "count": 1, "interface_nets": [],
    }]
    fixed, notes = validate_plan(plan, spec)
    mcu = next(b for b in fixed if b["id"] == "MCU")
    power = next(b for b in fixed if b["id"] == "POWER_REQUIREMENTS")
    assert mcu["roles"] == ["controller", "reset_button"]
    assert mcu["count"] == 1
    assert set(power["roles"]) == {
        "power_supply", "bulk_capacitor", "fuse", "tvss_diode"
    }
    assert power["count"] == 1
    assert any("restored omitted power roles" in note for note in notes)


def test_missing_block_ir_skipped():
    ir, notes = instantiate_blocks("t", PLAN, {}, rails=["GND"])
    assert not ir.components
    assert any("skipped" in n for n in notes)


def test_a_block_with_no_interface_net_is_an_island():
    """Measured on a real request (STM32G474 + 4 BLDC + 4 AS5048A + CAN + UART
    + battery monitor): MCU and COMM declared CAN_H/CAN_L/TX/RX and those four
    were exactly the signals that connected. MOTOR, ENCODER and BATTERY
    declared `[]` and the board came out with 100 one-pin nets out of 113.
    """
    from circuitgen.blocks import islands

    plan = [
        {"id": "MCU", "count": 1, "roles": ["controller"],
         "interface_nets": [{"name": "CAN_H", "purpose": "CAN high"}]},
        {"id": "MOTOR", "count": 4, "roles": ["motor_driver"], "interface_nets": []},
        {"id": "ENCODER", "count": 4, "roles": ["encoder"], "interface_nets": []},
    ]
    assert islands(plan) == ["MOTOR", "ENCODER"]

    # a single-block plan has nothing to be an island from
    assert islands([{"id": "ONLY", "count": 1, "interface_nets": []}]) == []


def test_a_repeated_block_gets_one_net_per_instance():
    """Measured on the 4-motor board: the plan correctly declared
    MOTOR{n}_PWM_A for count=4, the template IR came back naming it
    MOTOR1_PWM_A, and every instance then joined that same net — all four
    drivers on one PWM line, all four encoders on one chip select, and no
    MOTOR2/3/4 or ENC2/3/4 net anywhere on the board."""
    from circuitgen.blocks import instantiate_blocks
    from circuitgen.ir import CircuitIR, Component

    plan = [{
        "id": "MOTOR", "count": 4, "roles": ["motor_driver"],
        "interface_nets": [
            {"name": "MOTOR{n}_PWM_A", "purpose": "phase A"},
            {"name": "SPI_SCK", "purpose": "shared bus"},
        ],
    }]
    # the template as synthesis returns it: the per-instance net rendered
    # with a literal 1, the shared bus plain, plus a block-local net
    tmpl = CircuitIR("motor")
    tmpl.add(Component("U1", "X:DRV", "DRV"))
    tmpl.add(Component("C1", "Device:C", "100nF"))
    tmpl.connect("MOTOR1_PWM_A", ("U1", "15"))
    tmpl.connect("SPI_SCK", ("U1", "2"))
    tmpl.connect("VM_LOCAL", ("U1", "8"), ("C1", "1"))

    merged, _notes = instantiate_blocks("board", plan, {"MOTOR": tmpl}, ["GND"])
    names = {n.name: len(n.nodes) for n in merged.nets}

    # one PWM net per instance, one pin each
    assert {f"MOTOR{i}_PWM_A" for i in range(1, 5)} <= set(names)
    assert [names[f"MOTOR{i}_PWM_A"] for i in range(1, 5)] == [1, 1, 1, 1]
    # the shared bus stays shared: all four drivers on it
    assert names["SPI_SCK"] == 4
    # a block-local net is scoped per instance, not merged
    assert {f"MOTOR{i}_VM_LOCAL" for i in range(1, 5)} <= set(names)


def test_an_interface_net_that_misses_the_hub_is_still_dangling():
    """The MCU-wiring pass only offered a pin to nets holding exactly ONE pin,
    so a signal shared by four peripherals — reaching no controller pin at all
    — was never a candidate. Measured on the 4-motor board: nine MOTORn_*/
    ENCn_* nets, four pins each, zero MCU pins, and this pass skipped them.

    Runs the real pass with a model that refuses, so the deterministic
    round-robin fallback does the assignment.
    """
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()

    ir = CircuitIR("board")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "MCU"))
    for n in range(2, 6):
        ir.add(Component(f"U{n}", "Driver_Motor:DRV8311H", "DRV"))
    ir.connect("MOTOR_PWM", *[(f"U{n}", "15") for n in range(2, 6)])
    ir.connect("ALREADY_OK", ("U1", "20"), ("U2", "14"))

    notes = agent.wire_mcu_interfaces(
        ir, [{"net": "MOTOR_PWM"}, {"net": "ALREADY_OK"}]
    )
    assert any("MOTOR_PWM" in n for n in notes), notes
    pwm = next(n for n in ir.nets if n.name == "MOTOR_PWM")
    assert "U1" in {r for r, _ in pwm.nodes}, pwm.nodes
    # a net the hub already reaches is left alone
    assert not any("ALREADY_OK" in n for n in notes), notes


def _hub_agent():
    from circuitgen.agent import Agent
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()
    return agent


def test_a_package_too_small_for_the_board_is_grown_to_one_that_fits():
    """Measured: the model chose STM32G474CBTx (39 I/O) for a board whose plan
    needed 37 interface nets on the controller, every pin had been parked as a
    no-connect, and this pass returned an empty list without a word.

    "STM32G474" names a family, not a package. Counting the pins the plan
    requires and picking the smallest package that carries them is arithmetic,
    and it is the design work the user says they cannot do.
    """
    from circuitgen.ir import CircuitIR, Component

    agent = _hub_agent()
    ir = CircuitIR("too-small")
    hub_id = "MCU_ST_STM32G4:STM32G474CBTx"
    ir.add(Component("U1", hub_id, "STM32G474"))
    ir.add(Component("U2", "Driver_Motor:DRV8311H", "DRV"))
    sym = agent.parts.load_symbols([hub_id])[hub_id]
    io = [p for p in sym.pins if p.etype.name in ("BIDIR", "INPUT", "OUTPUT")]
    # more interface nets than this package has I/O pins
    catalog = [{"net": f"SIG{i}"} for i in range(len(io) + 4)]
    for c in catalog:
        ir.connect(c["net"], ("U2", "15"))

    notes = agent.wire_mcu_interfaces(ir, catalog)
    grown = ir.components["U1"].lib_id
    assert grown != hub_id, notes
    new_io = len([
        p for p in agent.parts.load_symbols([grown])[grown].pins
        if p.etype.name in ("BIDIR", "INPUT", "OUTPUT")
    ])
    assert new_io >= len(catalog)
    assert any("was replaced by" in n and str(new_io) in n for n in notes), notes
    # and it took the SMALLEST that fits, not the biggest in the family
    assert new_io < 87, f"{grown} is larger than this board needs"


def test_when_no_package_in_the_family_fits_it_says_what_to_decide():
    """A shortfall no part can absorb is the user's call — split the board or
    drop a peripheral — and the run must say so instead of going quiet."""
    from circuitgen.ir import CircuitIR, Component

    agent = _hub_agent()
    ir = CircuitIR("hopeless")
    hub_id = "MCU_ST_STM32G4:STM32G474CBTx"
    ir.add(Component("U1", hub_id, "STM32G474"))
    ir.add(Component("U2", "Driver_Motor:DRV8311H", "DRV"))
    catalog = [{"net": f"SIG{i}"} for i in range(400)]
    for c in catalog:
        ir.connect(c["net"], ("U2", "15"))

    notes = agent.wire_mcu_interfaces(ir, catalog)
    assert ir.components["U1"].lib_id == hub_id, "nothing fits, so nothing moves"
    joined = " ".join(notes)
    assert "no package of" in joined and "the largest available is" in joined
    assert "Split the board" in joined


def test_a_duplicate_reference_is_renamed_not_fatal():
    """Measured: the model wrote BATMON1 twice, CircuitIR.add raised, the block
    was retried with the identical prompt at temperature 0 so it failed
    identically, and the user got NO schematic at all — for a name collision.
    A board you can look at beats an error message."""
    from circuitgen.ir_json import ir_from_json

    data = {
        "name": "batmon",
        "components": [
            {"ref": "BATMON1", "lib_id": "Device:Battery", "value": "Battery"},
            {"ref": "BATMON1", "lib_id": "Device:R", "value": "10k"},
        ],
        "nets": [{"name": "BAT_V", "nodes": [{"ref": "BATMON1", "pin": "1"}]}],
        "nc_pins": [],
    }
    notes: list[str] = []
    ir = ir_from_json(data, notes)
    assert set(ir.components) == {"BATMON1", "BATMON2"}
    assert ir.components["BATMON1"].lib_id == "Device:Battery"  # first keeps it
    assert any("renamed to BATMON2" in n for n in notes), notes
    # the nets stay with the first, so the copy is visibly unconnected
    assert [n.nodes for n in ir.nets] == [[("BATMON1", "1")]]


def test_the_nets_the_model_did_not_assign_are_wired_anyway():
    """Measured: 36 interface nets needed a controller pin, the assignment
    schema caps the model at 24, and the deterministic fallback only ran when
    the model returned NOTHING. Twelve nets were left alone on their nets in
    silence — SCK1..4, PWM_C1..4, CAN_TX/RX — and every one of the nine
    blocking issues on that run traced back to this."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    hub_id = "MCU_ST_STM32G4:STM32G474RETx"

    class AnswersOnlyTwo:
        def complete_json(self, messages, schema, **kw):
            return {"assignments": [{"net": "SIG0", "pin": "20"},
                                    {"net": "SIG1", "pin": "21"}]}

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = AnswersOnlyTwo()

    ir = CircuitIR("partial")
    ir.add(Component("U1", hub_id, "STM32G474"))
    ir.add(Component("U2", "Driver_Motor:DRV8311H", "DRV"))
    catalog = [{"net": f"SIG{i}"} for i in range(8)]
    for c in catalog:
        ir.connect(c["net"], ("U2", "15"))

    notes = agent.wire_mcu_interfaces(ir, catalog)
    wired = {
        net.name for net in ir.nets if "U1" in {r for r, _ in net.nodes}
    }
    assert wired == {f"SIG{i}" for i in range(8)}, wired
    assert any("model left it unassigned" in n for n in notes), notes


def test_a_signal_pin_alone_on_its_net_is_offered_a_controller_pin():
    """Measured: the plan declared the CAN BUS (CAN_H/CAN_L) as the COMM
    block's interface, so CAN_TX and CAN_RX — the transceiver's logic side,
    the pins an MCU actually drives — were never candidates and stayed alone
    on their nets to the end. A signal pin alone on its net reaches nothing,
    which is the same fact analyze_conduction reports as a dead component.

    A net holding only the HUB is left alone: its peripheral is missing, and a
    second controller pin would not fix that.
    """
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()

    ir = CircuitIR("logic-side")
    ir.add(Component("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474"))
    ir.add(Component("U10", "Interface_CAN_LIN:TJA1051T", "TJA1051T"))
    ir.connect("CAN_TX", ("U10", "1"))          # TXD, INPUT, alone
    ir.connect("CAN_RX", ("U10", "4"))          # RXD, OUTPUT, alone
    ir.connect("UART_TX", ("U1", "24"))         # only the hub — not our problem
    ir.connect("VCC_ONLY", ("U10", "3"))        # PWRIN, not a signal pin

    notes = agent.wire_mcu_interfaces(ir, [{"net": "CAN_H"}])
    on = lambda name: {r for net in ir.nets if net.name == name for r, _ in net.nodes}
    assert "U1" in on("CAN_TX") and "U1" in on("CAN_RX"), notes
    assert on("UART_TX") == {"U1"}, "a net holding only the hub gains nothing"
    assert on("VCC_ONLY") == {"U10"}, "a supply pin is not offered a GPIO"


def test_four_roles_of_one_are_four_instances_not_one():
    """Measured with the gemma model: the spec expressed four motors as four
    ROLES — BLDC_Motor_1..4, each quantity 1 — instead of one role with
    quantity 4. Both describe the same board. max() read only the first, so
    the block that owned all four came out count=1, three of the four drivers
    were deleted as duplicates, and a four-motor request produced one motor.

    Roles asking for the SAME PART are one repeated channel and add up; roles
    asking for different parts (a controller and its reset button) sit in one
    instance together and must not.
    """
    from circuitgen.blocks import validate_plan

    spec = {"parts_needed": [
        {"role": "STM32G474_MCU", "search_query": "STM32G474", "quantity": 1},
        {"role": "reset_button", "search_query": "tactile switch", "quantity": 1},
        *[{"role": f"BLDC_Motor_{i}", "search_query": "BLDC motor", "quantity": 1}
          for i in range(1, 5)],
        {"role": "encoder", "search_query": "AS5048A", "quantity": 4},
    ]}
    plan = [
        {"id": "MCU", "count": 1, "roles": ["STM32G474_MCU", "reset_button"],
         "interface_nets": [{"name": "SWDIO"}]},
        {"id": "MOTOR", "count": 1,
         "roles": [f"BLDC_Motor_{i}" for i in range(1, 5)],
         "interface_nets": [{"name": "PWM{n}"}]},
        {"id": "ENC", "count": 1, "roles": ["encoder"],
         "interface_nets": [{"name": "CS{n}"}]},
    ]
    fixed, notes = validate_plan(plan, spec)
    counts = {b["id"]: b["count"] for b in fixed}
    assert counts["MOTOR"] == 4, notes      # four roles, one part
    assert counts["ENC"] == 4, notes        # one role, quantity four
    assert counts["MCU"] == 1, notes        # two roles, two different parts


def test_a_repeated_block_template_holds_one_channel_not_all_of_them():
    """Measured right after the count fix landed: the MOTOR block correctly
    became count=4, and then the completeness gate demanded four motors inside
    its single template — "4 uncatalogued role(s) but only 2 conceptual
    device(s)" — twice, and the run stopped with no schematic at all.

    A repeated block is synthesized once and stamped count times, so its
    template is ONE channel. A count=1 block, and the pseudo-block the flat
    path checks the whole circuit against, must still hold every role.
    """
    from circuitgen.blocks import validate_block_template
    from circuitgen.ir import CircuitIR, Component

    roles = {f"BLDC_Motor_{i}": "BLDC motor" for i in range(1, 5)}
    ir = CircuitIR("template")
    ir.add(Component("MOTOR1", "Conceptual:motor", "motor"))

    repeated = {"id": "MOTOR", "count": 4, "roles": list(roles)}
    assert validate_block_template(repeated, ir, {}, roles) == []

    flat = {"id": "CIRCUIT", "count": 1, "roles": list(roles)}
    issues = validate_block_template(flat, ir, {}, roles)
    assert issues and "4 uncatalogued part(s)" in issues[0], issues

    # two different uncatalogued parts in one repeated block still need two
    mixed = {"id": "MIXED", "count": 4,
             "roles": ["BLDC_Motor_1", "servo"], }
    issues = validate_block_template(
        mixed, ir, {}, {"BLDC_Motor_1": "BLDC motor", "servo": "STS3215 servo"}
    )
    assert issues and "2 uncatalogued part(s)" in issues[0], issues


def test_a_package_swap_does_not_make_the_role_look_missing():
    """Measured on the Qwen run: the hub was grown from STM32G474CBTx to RBTx
    because the board needed 50 I/O pins, and from that moment the gate said
    "required role 'controller' has no catalog device" — the new lib_id was
    never in the candidate list — so EVERY repair round was rejected and four
    MCU VDD pins stayed on signal nets with no chance of being fixed.

    The gate accepts what the sizer is allowed to do and nothing looser: same
    library, and only the package/ordering suffix may differ. A plain shared
    prefix is not enough — STM32G474 and STM32G431 agree on seven characters
    and are different parts.
    """
    from circuitgen.blocks import validate_block_template
    from circuitgen.ir import CircuitIR, Component

    block = {"id": "CIRCUIT", "count": 1, "roles": ["controller"]}
    cands = {"controller": [{"lib_id": "MCU_ST_STM32G4:STM32G474CBTx"}]}

    def check(lib):
        ir = CircuitIR("t")
        ir.add(Component("U1", lib, "x"))
        return validate_block_template(block, ir, cands)

    assert check("MCU_ST_STM32G4:STM32G474RBTx") == []   # grown package
    assert check("MCU_ST_STM32G4:STM32G474RETx") == []   # another package
    assert check("MCU_ST_STM32G4:STM32G431CBTx")         # different family
    assert check("Device:R")                             # not a controller


def test_hub_joins_an_i2c_bus_that_already_has_sensor_header_and_pullup():
    """A 4-role I2C board never crosses BLOCK_THRESHOLD, so the merge-time
    wire_mcu_interfaces call never ran. SDA already had three members — not a
    single-pin net — and the MCU had only supply pins on the board.
    """
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()

    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    sensor = "Sensor_Temperature:TMP100"
    ir = CircuitIR("i2c")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", sensor, "TMP100"))
    ir.add(Component("R3", "Device:R", "10k"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.connect("+3V3", ("U1", "16"), ("U2", "4"), ("R3", "2"), ("J1", "1"))
    ir.connect("GND", ("U1", "15"), ("U2", "2"), ("J1", "4"))
    ir.connect("SDA", ("U2", "6"), ("J1", "2"), ("R3", "1"))
    ir.connect("SCL", ("U2", "1"), ("J1", "3"))

    notes = agent._join_hub_to_i2c_buses(ir)
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert "U1" in {r for r, _ in sda.nodes}, notes
    assert "U1" in {r for r, _ in scl.nodes}, notes
    from circuitgen.pinfunctions import resolve_function_pin
    from circuitgen.partindex import PartIndex as PI

    sym = PI().load_symbols([mcu])[mcu]
    sda_pin = resolve_function_pin(mcu, sym, "I2C1_SDA")[0]
    scl_pin = resolve_function_pin(mcu, sym, "I2C1_SCL")[0]
    assert ("U1", sda_pin) in sda.nodes, notes
    assert ("U1", scl_pin) in scl.nodes, notes
    # the previous pass round-robined free I/O: pin 2 is PC13, not I2C
    assert ("U1", "2") not in sda.nodes and ("U1", "2") not in scl.nodes
    assert any("DS12288" in n for n in notes), notes
    assert agent._join_hub_to_i2c_buses(ir) == []


def test_hub_gpio_already_on_i2c_is_moved_to_a_recorded_af_pin():
    """The model (or the old round-robin) can put PC13 on SDA. That pin has
    no I2C AF; leaving it because the hub is 'already on the net' is the
    same dead-board class as EN=NC."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex
    from circuitgen.pinfunctions import resolve_function_pin

    agent = object.__new__(Agent)
    agent.parts = PartIndex()

    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    sensor = "Sensor_Temperature:TMP100"
    ir = CircuitIR("i2c")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", sensor, "TMP100"))
    ir.connect("SDA", ("U1", "2"), ("U2", "6"))  # PC13
    ir.connect("SCL", ("U2", "1"))

    notes = agent._join_hub_to_i2c_buses(ir)
    sda = next(n for n in ir.nets if n.name == "SDA")
    sym = agent.parts.load_symbols([mcu])[mcu]
    sda_pin = resolve_function_pin(mcu, sym, "I2C1_SDA")[0]
    assert ("U1", sda_pin) in sda.nodes, notes
    assert ("U1", "2") not in sda.nodes, notes
    assert ("U1", "2") in ir.nc_pins, notes
    assert any("moved U1.2" in n for n in notes), notes


def test_hub_without_recorded_i2c_af_is_not_wired_to_a_gpio():
    """No datasheet row means the bus stays without a hub pin. Guessing a
    free GPIO is how PC13 landed on SDA."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    class Refuses:
        def complete_json(self, *a, **k):
            raise RuntimeError("no model in this test")

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    agent.llm = Refuses()

    mcu = "RF_Module:ESP32-WROOM-32"
    sensor = "Sensor_Temperature:TMP100"
    ir = CircuitIR("i2c")
    ir.add(Component("U1", mcu, "ESP32"))
    ir.add(Component("U2", sensor, "TMP100"))
    ir.add(Component("R3", "Device:R", "10k"))
    ir.connect("+3V3", ("U1", "3"), ("U2", "4"), ("R3", "2"))
    ir.connect("GND", ("U1", "1"), ("U2", "2"))
    ir.connect("SDA", ("U2", "6"), ("R3", "1"))
    ir.connect("SCL", ("U2", "1"))

    notes = agent._join_hub_to_i2c_buses(ir)
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert "U1" not in {r for r, _ in sda.nodes}, notes
    assert "U1" not in {r for r, _ in scl.nodes}, notes
    assert any("no recorded I2C" in n for n in notes), notes


def test_i2c_af_checker_agrees_with_the_join_pass():
    """GPIO on SDA is a failure; after the join, it is not. One definition."""
    from circuitgen.agent import Agent
    from circuitgen.erc import i2c_hub_af_failures
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    sensor = "Sensor_Temperature:TMP100"
    ir = CircuitIR("i2c")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", sensor, "TMP100"))
    ir.connect("SDA", ("U1", "2"), ("U2", "6"))  # PC13
    ir.connect("SCL", ("U2", "1"))
    symbols = agent._resolve_symbols(ir)
    before = i2c_hub_af_failures(ir, symbols)
    assert any("U1.2" in path or "not a recorded SDA" in msg for path, msg in before), before
    agent._join_hub_to_i2c_buses(ir)
    assert i2c_hub_af_failures(ir, agent._resolve_symbols(ir)) == []


def _pin_named(symbols, lib_id, token: str) -> str:
    want = token.upper()
    for pin in symbols[lib_id].pins:
        raw = (pin.name or "").replace("~", "").replace("{", "").replace("}", "")
        first = raw.split("/")[0].strip().upper()
        if first == want:
            return pin.number
    raise AssertionError(f"no {token} pin on {lib_id}")


def _w25q_pin(symbols, token: str) -> str:
    return _pin_named(symbols, "Memory_Flash:W25Q32JVSS", token)


def test_hub_joins_spi_from_named_bus_lines_when_flash_is_already_on_them():
    """W25Q pins are CLK/DI/DO, not SCK. The net names are the bus when
    the flash is a member and the MCU is not yet."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex
    from circuitgen.pinfunctions import resolve_function_ending

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    flash = "Memory_Flash:W25Q32JVSS"
    ir = CircuitIR("spi")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", flash, "W25Q32JVSS"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "SPI"))
    symbols = agent._resolve_symbols(ir)
    clk = _w25q_pin(symbols, "CLK")
    di = _w25q_pin(symbols, "DI")
    do = _w25q_pin(symbols, "DO")
    cs = _w25q_pin(symbols, "CS")
    ir.connect("SCK", ("U2", clk), ("J1", "1"))
    ir.connect("MOSI", ("U2", di), ("J1", "2"))
    ir.connect("MISO", ("U2", do), ("J1", "3"))
    ir.connect("NSS", ("U2", cs), ("J1", "4"))
    notes = agent._join_hub_to_i2c_buses(ir)
    sck = resolve_function_ending(mcu, symbols[mcu], "SCK")[0]
    mosi = resolve_function_ending(mcu, symbols[mcu], "MOSI")[0]
    miso = resolve_function_ending(mcu, symbols[mcu], "MISO")[0]
    nss = resolve_function_ending(mcu, symbols[mcu], "NSS")[0]
    by = {n.name: {r: p for r, p in n.nodes} for n in ir.nets}
    assert by["SCK"].get("U1") == sck, notes
    assert by["MOSI"].get("U1") == mosi, notes
    assert by["MISO"].get("U1") == miso, notes
    assert by["NSS"].get("U1") == nss, notes
    assert ("U1", "2") not in ir.nets[0].nodes
    assert any("DS12288" in n for n in notes), notes
    assert agent._join_hub_to_i2c_buses(ir) == []


def test_hub_gpio_on_spi_clock_is_moved_to_a_recorded_af_pin():
    """25LC names the clock SCK — Table 12's suffix, not a CLK alias."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex
    from circuitgen.pinfunctions import resolve_function_ending

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    eeprom = "Memory_EEPROM:25LCxxx-MC"
    ir = CircuitIR("spi-gpio")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", eeprom, "25LC"))
    symbols = agent._resolve_symbols(ir)
    ir.connect("FOO", ("U1", "2"), ("U2", _pin_named(symbols, eeprom, "SCK")))
    notes = agent._join_hub_to_i2c_buses(ir)
    sck = resolve_function_ending(mcu, symbols[mcu], "SCK")[0]
    net = next(n for n in ir.nets if n.name == "FOO")
    assert ("U1", sck) in net.nodes, notes
    assert ("U1", "2") not in net.nodes
    assert ("U1", "2") in ir.nc_pins


def test_gpio_chip_select_is_not_moved_off_spi_cs():
    """Software CS is a GPIO. The join must not steal it for SPI1_NSS."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    eeprom = "Memory_EEPROM:25LCxxx-MC"
    ir = CircuitIR("spi-cs")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", eeprom, "25LC"))
    symbols = agent._resolve_symbols(ir)
    ir.connect("CS", ("U1", "2"), ("U2", _pin_named(symbols, eeprom, "CS")))
    agent._join_hub_to_i2c_buses(ir)
    net = next(n for n in ir.nets if n.name == "CS")
    assert ("U1", "2") in net.nodes


def test_spi_af_checker_agrees_with_the_join_pass():
    from circuitgen.agent import Agent
    from circuitgen.erc import spi_hub_af_failures
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    eeprom = "Memory_EEPROM:25LCxxx-MC"
    ir = CircuitIR("spi-chk")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", eeprom, "25LC"))
    symbols = agent._resolve_symbols(ir)
    ir.connect("FOO", ("U1", "2"), ("U2", _pin_named(symbols, eeprom, "SCK")))
    before = spi_hub_af_failures(ir, symbols)
    assert any("not a recorded SCK" in msg for _p, msg in before), before
    agent._join_hub_to_i2c_buses(ir)
    assert spi_hub_af_failures(ir, agent._resolve_symbols(ir)) == []


def test_unrecorded_hub_on_spi_is_not_an_af_failure():
    from circuitgen.erc import spi_hub_af_failures
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    mcu = "RF_Module:ESP32-WROOM-32"
    flash = "Memory_Flash:W25Q32JVSS"
    symbols = parts.load_symbols([mcu, flash])
    ir = CircuitIR("esp-spi")
    ir.add(Component("U1", mcu, "ESP32"))
    ir.add(Component("U2", flash, "W25Q32"))
    ir.connect("SCK", ("U1", "18"), ("U2", _w25q_pin(symbols, "CLK")))
    assert spi_hub_af_failures(ir, symbols) == []


def test_a_counter_clk_on_foo_is_not_spi():
    """4017 pin 14 is named CLK. That is not SPI1_SCK."""
    from circuitgen.agent import Agent
    from circuitgen.erc import is_spi_net
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    ir = CircuitIR("4017-clk")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", "4xxx:4017", "4017"))
    ir.connect("FOO", ("U1", "2"), ("U2", "14"))  # PC13, CLK
    symbols = agent._resolve_symbols(ir)
    net = next(n for n in ir.nets if n.name == "FOO")
    assert not is_spi_net(ir, symbols, net)
    assert agent._join_hub_to_i2c_buses(ir) == []
    assert ("U1", "2") in net.nodes
    ir2 = CircuitIR("4017-sck")
    ir2.add(Component("U1", mcu, "STM32G474RET6"))
    ir2.add(Component("U2", "4xxx:4017", "4017"))
    ir2.connect("SCK", ("U1", "2"), ("U2", "14"))
    symbols2 = agent._resolve_symbols(ir2)
    sck = next(n for n in ir2.nets if n.name == "SCK")
    assert not is_spi_net(ir2, symbols2, sck)
    assert agent._join_hub_to_i2c_buses(ir2) == []
    assert ("U1", "2") in sck.nodes


def test_flash_clk_on_an_unlabeled_net_is_not_spi():
    """W25Q CLK on FOO is the same token as 4017 CLK. Without a cited
    flash table it is not SCK."""
    from circuitgen.agent import Agent
    from circuitgen.erc import is_spi_net
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    flash = "Memory_Flash:W25Q32JVSS"
    ir = CircuitIR("flash-foo")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("U2", flash, "W25Q32JVSS"))
    symbols = agent._resolve_symbols(ir)
    ir.connect("FOO", ("U1", "2"), ("U2", _w25q_pin(symbols, "CLK")))
    net = next(n for n in ir.nets if n.name == "FOO")
    assert not is_spi_net(ir, symbols, net)
    assert agent._join_hub_to_i2c_buses(ir) == []
    assert ("U1", "2") in net.nodes
    ir2 = CircuitIR("flash-sck")
    ir2.add(Component("U1", mcu, "STM32G474RET6"))
    ir2.add(Component("U2", flash, "W25Q32JVSS"))
    symbols2 = agent._resolve_symbols(ir2)
    ir2.connect("SCK", ("U1", "2"), ("U2", _w25q_pin(symbols2, "CLK")))
    sck = next(n for n in ir2.nets if n.name == "SCK")
    assert not is_spi_net(ir2, symbols2, sck)
    assert agent._join_hub_to_i2c_buses(ir2) == []
    assert ("U1", "2") in sck.nodes


def test_a_two_pin_header_and_a_three_pin_header_on_sck_are_the_same():
    """Same membership (MCU GPIO + one header pad). Pin count is not a bus."""
    from circuitgen.agent import Agent
    from circuitgen.erc import is_spi_net
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"

    def board(header: str) -> tuple:
        ir = CircuitIR(header)
        ir.add(Component("U1", mcu, "STM32G474RET6"))
        ir.add(Component("J1", header, "H"))
        ir.connect("SCK", ("U1", "2"), ("J1", "1"))
        symbols = agent._resolve_symbols(ir)
        net = next(n for n in ir.nets if n.name == "SCK")
        return is_spi_net(ir, symbols, net), agent._join_hub_to_i2c_buses(ir), net

    two = board("Connector_Generic:Conn_01x02")
    three = board("Connector_Generic:Conn_01x03")
    assert two[0] is False and three[0] is False
    assert two[1] == [] and three[1] == []
    assert ("U1", "2") in two[2].nodes and ("U1", "2") in three[2].nodes


def test_a_gpio_and_resistor_on_a_net_called_sck_is_not_spi():
    """STM32 PC13 plus a resistor on a net labelled SCK is still GPIO.

    The electrically identical net named FOO was not SPI; treating the
    label as a bus made the join steal PC13 for PA5.
    """
    from circuitgen.agent import Agent
    from circuitgen.erc import is_i2c_net, is_spi_net, spi_hub_af_failures
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    ir = CircuitIR("label-sck")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("R1", "Device:R", "10k"))
    ir.connect("SCK", ("U1", "2"), ("R1", "1"))  # PC13
    ir.connect("SDA", ("U1", "3"), ("R1", "2"))  # PC14
    symbols = agent._resolve_symbols(ir)
    sck = next(n for n in ir.nets if n.name == "SCK")
    sda = next(n for n in ir.nets if n.name == "SDA")
    assert not is_spi_net(ir, symbols, sck)
    assert not is_i2c_net(ir, symbols, sda)
    assert spi_hub_af_failures(ir, symbols) == []
    notes = agent._join_hub_to_i2c_buses(ir)
    assert notes == []
    assert ("U1", "2") in sck.nodes
    assert ("U1", "3") in sda.nodes


def test_a_recorded_sck_pin_on_an_unlabeled_net_is_still_spi():
    """PA5 on FOO is SPI1_SCK in DS12288 Table 12 even when the net is FOO."""
    from circuitgen.agent import Agent
    from circuitgen.erc import is_spi_net, spi_hub_af_failures, spi_line_role
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex
    from circuitgen.pinfunctions import resolve_function_ending

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    ir = CircuitIR("af-sck")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("R1", "Device:R", "10k"))
    symbols = agent._resolve_symbols(ir)
    sck = resolve_function_ending(mcu, symbols[mcu], "SCK")[0]
    ir.connect("FOO", ("U1", sck), ("R1", "1"))
    net = next(n for n in ir.nets if n.name == "FOO")
    assert spi_line_role(ir, symbols, net) == "SCK"
    assert is_spi_net(ir, symbols, net)
    assert spi_hub_af_failures(ir, symbols) == []
    assert agent._join_hub_to_i2c_buses(ir) == []
    assert ("U1", sck) in net.nodes


def test_esp32_net_named_sda_is_still_i2c():
    """ESP32 IO21 is not named SDA and has no AF table. The net name is the
    evidence the pull-up checker already shared with the join."""
    from circuitgen.erc import is_i2c_net
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex

    parts = PartIndex()
    mcu = "RF_Module:ESP32-WROOM-32"
    symbols = parts.load_symbols([mcu, "Device:R"])
    ir = CircuitIR("esp-sda")
    ir.add(Component("U1", mcu, "ESP32"))
    ir.add(Component("R3", "Device:R", "10k"))
    ir.connect("SDA", ("U1", "33"), ("R3", "2"))  # IO21
    net = next(n for n in ir.nets if n.name == "SDA")
    assert is_i2c_net(ir, symbols, net)


def test_header_and_pullup_on_sda_still_gets_the_hub():
    """A labelled SDA with no recorded device on it is still a bus — the
    MCU is not a member yet, so the label is the only evidence."""
    from circuitgen.agent import Agent
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.partindex import PartIndex
    from circuitgen.pinfunctions import resolve_function_pin

    agent = object.__new__(Agent)
    agent.parts = PartIndex()
    mcu = "MCU_ST_STM32G4:STM32G474RETx"
    ir = CircuitIR("header-sda")
    ir.add(Component("U1", mcu, "STM32G474RET6"))
    ir.add(Component("R3", "Device:R", "10k"))
    ir.add(Component("J1", "Connector_Generic:Conn_01x04", "I2C"))
    ir.connect("SDA", ("J1", "2"), ("R3", "1"))
    ir.connect("SCL", ("J1", "3"))
    notes = agent._join_hub_to_i2c_buses(ir)
    symbols = agent._resolve_symbols(ir)
    sda_pin = resolve_function_pin(mcu, symbols[mcu], "I2C1_SDA")[0]
    scl_pin = resolve_function_pin(mcu, symbols[mcu], "I2C1_SCL")[0]
    sda = next(n for n in ir.nets if n.name == "SDA")
    scl = next(n for n in ir.nets if n.name == "SCL")
    assert ("U1", sda_pin) in sda.nodes, notes
    assert ("U1", scl_pin) in scl.nodes, notes
