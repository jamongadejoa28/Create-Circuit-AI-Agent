"""JSON Schemas for every LLM interaction.

Every model reply is forced through one of these via llama-server's
json_schema constrained decoding — malformed structure is impossible by
construction; content errors (unknown parts/pins) are caught by the
deterministic validators afterward.

Kept deliberately small: these schemas travel in every request, and the
context budget is approximately 8k tokens.
"""

REQUIREMENT_SPEC = {
    "type": "object",
    "required": ["mode", "summary", "power", "parts_needed", "connections_intent"],
    "additionalProperties": False,
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["transcription", "design"],
            "description": (
                "transcription only when the request supplies explicit net members; "
                "design when it asks the system to decide connections or values"
            ),
        },
        "summary": {"type": "string", "maxLength": 160, "description": "one-line normalized requirement"},
        "power": {
            "type": "object",
            "required": ["rails"],
            "additionalProperties": False,
            "properties": {
                "rails": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "voltage"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "description": "e.g. +5V, +3V3, GND"},
                            "voltage": {"type": "string", "maxLength": 12},
                        },
                    },
                }
            },
        },
        "parts_needed": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["role", "search_query"],
                "additionalProperties": False,
                "properties": {
                    "role": {"type": "string", "description": "short role id, e.g. mcu, led1, btn1"},
                    "functional_kind": {
                        "type": "string",
                        "enum": [
                            "voltage_regulator", "input_bypass_capacitor",
                            "output_bypass_capacitor", "decoupling_capacitor",
                            "resistor", "capacitor", "inductor", "diode", "led",
                            "switch", "connector", "transistor", "relay",
                            "operational_amplifier", "microcontroller", "sensor",
                            "memory", "communication_interface", "motor_driver",
                            "shunt_voltage_reference", "integrated_circuit", "other"
                        ],
                        "description": (
                            "typed electrical function used for design-rule matching; "
                            "classify what this physical part does in this circuit"
                        ),
                    },
                    "reference": {"type": "string", "maxLength": 8,
                                  "description": "the designator the REQUEST gives this part (U1, J2, C3); omit if the request does not name one"},
                    "search_query": {"type": "string", "maxLength": 48, "description": "part-index search terms, English"},
                    "value": {"type": "string", "maxLength": 24, "description": "component value if applicable, e.g. 330R"},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 16,
                                 "description": "physical copies requested; default 1"},
                    "polarized": {
                        "type": "boolean",
                        "description": "true only when explicitly polarized or electrolytic",
                    },
                },
            },
        },
        "signals": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["name"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "maxLength": 24,
                             "description": "net name, e.g. TX, RX, SDA, SCL, CANH"},
                    "purpose": {"type": "string", "maxLength": 80},
                },
            },
            "description": (
                "Interface SIGNALS the board must expose. A signal is a net, not a "
                "part to buy: TX, RX, SDA, an interrupt line and a chip select all "
                "belong here and NEVER in parts_needed."
            ),
        },
        "netlist": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "required": ["name", "nodes"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "maxLength": 24},
                    "nodes": {
                        "type": "array",
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "required": ["reference", "pin"],
                            "additionalProperties": False,
                            "properties": {
                                "reference": {"type": "string", "maxLength": 8},
                                "pin": {"type": "string", "maxLength": 12,
                                        "description": "pin NUMBER when the request gives one, else its name (VOUT, K, A)"},
                                "pin_name": {"type": "string", "maxLength": 24,
                                             "description": "the pin name written after the number, e.g. VCC in 8:VCC; empty when absent"},
                            },
                        },
                    },
                },
            },
            "description": (
                "TRANSCRIBE a net list the request already contains, exactly as "
                "written — every net, every reference, every pin. Leave EMPTY when "
                "the request describes what the circuit should do rather than "
                "listing its connections. Never invent a connection here."
            ),
        },
        "connections_intent": {
            "type": "array",
            "items": {"type": "string", "maxLength": 140},
            "description": "plain statements like 'btn1 between +5V and led1 anode via resistor'",
        },
        "constraints": {"type": "array", "items": {"type": "string", "maxLength": 120}},
        "out_of_scope": {
            "type": "boolean",
            "description": "true if the request exceeds 24VDC/3A, mains, isolation, or safety-critical scope",
        },
        "out_of_scope_reason": {"type": "string", "maxLength": 200},
    },
}


CIRCUIT_IR = {
    "type": "object",
    "required": ["name", "components", "nets"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+$"},
        "components": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "required": ["ref", "lib_id", "value"],
                "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 12, "pattern": "^#?[A-Za-z]+[0-9]+$"},
                    "lib_id": {"type": "string", "maxLength": 64, "description": "EXACT id from the candidate list"},
                    "value": {"type": "string", "maxLength": 24},
                    "footprint": {"type": "string", "maxLength": 64},
                },
            },
        },
        "nets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "nodes"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "nodes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["ref", "pin"],
                            "additionalProperties": False,
                            "properties": {
                                "ref": {"type": "string"},
                                "pin": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "nc_pins": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ref", "pin"],
                "additionalProperties": False,
                "properties": {"ref": {"type": "string"}, "pin": {"type": "string"}},
            },
        },
    },
}


# Per-op variants: required fields are enforced per operation so the model
# cannot emit e.g. a connect without a net (observed with the 7B model
# under a permissive schema).
_OP_VARIANTS = [
    {
        "type": "object",
        "required": ["op", "ref", "lib_id"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "add_component"},
            "ref": {"type": "string"},
            "lib_id": {"type": "string"},
            "value": {"type": "string"},
            "footprint": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref"],
        "additionalProperties": False,
        "properties": {"op": {"const": "remove_component"}, "ref": {"type": "string"}},
    },
    {
        "type": "object",
        "required": ["op", "ref", "pin", "net"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "connect"},
            "ref": {"type": "string"},
            "pin": {"type": "string"},
            "net": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref", "pin", "net"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "disconnect"},
            "ref": {"type": "string"},
            "pin": {"type": "string"},
            "net": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref", "pin"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "set_nc"},
            "ref": {"type": "string"},
            "pin": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref", "pin"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "clear_nc"},
            "ref": {"type": "string"},
            "pin": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref", "value"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "set_value"},
            "ref": {"type": "string"},
            "value": {"type": "string"},
        },
    },
    {
        "type": "object",
        "required": ["op", "ref", "footprint"],
        "additionalProperties": False,
        "properties": {
            "op": {"const": "set_footprint"},
            "ref": {"type": "string"},
            "footprint": {"type": "string"},
        },
    },
]

REPAIR_PATCH = {
    "type": "object",
    "required": ["ops"],
    "additionalProperties": False,
    "properties": {
        "analysis": {"type": "string", "maxLength": 200, "description": "one sentence on the root cause"},
        "ops": {"type": "array", "maxItems": 12, "items": {"anyOf": _OP_VARIANTS}},
    },
}


BLOCK_PLAN = {
    "type": "object",
    "required": ["blocks"],
    "additionalProperties": False,
    "properties": {
        "blocks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["id", "description", "roles", "count", "interface_nets"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "maxLength": 12, "pattern": "^[A-Z][A-Z0-9]*$"},
                    "description": {"type": "string", "maxLength": 100},
                    "roles": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 32},
                        "description": "role ids from spec.parts_needed handled by this block",
                    },
                    "count": {"type": "integer", "minimum": 1, "maximum": 8},
                    "interface_nets": {
                        "type": "array",
                        "maxItems": 14,
                        "items": {
                            "type": "object",
                            "required": ["name", "purpose", "peer", "protocol", "required"],
                            "additionalProperties": False,
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "maxLength": 24,
                                    "description": "net shared with other blocks; use literal {n} for per-instance nets of repeated blocks, e.g. ENC{n}_CS",
                                },
                                "purpose": {"type": "string", "maxLength": 60},
                                "peer": {
                                    "type": "string",
                                    "enum": ["controller", "external", "block"],
                                    "description": "required endpoint at the other side of this block interface",
                                },
                                "protocol": {
                                    "type": "string",
                                    "enum": ["i2c", "spi", "uart", "can", "generic_control", "other"],
                                    "description": "typed interface context; PWM/DIR/FAULT are generic_control",
                                },
                                "required": {
                                    "type": "boolean",
                                    "description": "whether the peer endpoint is required for this design",
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


#: the net list on its own. Asking for everything at once let a model fill
#: every `reference` and still leave `netlist` empty (measured: the NE555
#: request, whose connections were listed as plainly as the two that worked).
#: One question, one answer.
NETLIST_ONLY = {
    "type": "object",
    "required": ["parts", "netlist"],
    "additionalProperties": False,
    "properties": {
        "parts": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "required": ["reference", "part", "value", "package", "polarized"],
                "additionalProperties": False,
                "properties": {
                    "reference": {"type": "string", "maxLength": 8,
                                  "description": "the designator: U1, R3, C2, J1"},
                    "part": {"type": "string", "maxLength": 48,
                             "description": "PART ID OR CATALOG TYPE ONLY: AMS1117-3.3, NE555D, resistor, capacitor, LED, 1x2 pin header, 2x3 pin header. Connector dimensions belong here. Never put a passive electrical value such as 10uF, 4.7k or 330R here"},
                    "value": {"type": "string", "maxLength": 24,
                              "description": "REQUIRED string: the electrical value or marking printed on this exact reference (10uF, 1k, 22pF, green); connector dimensions such as 1x2 and 2x3 are part types, not values; use an empty string when no printed value is given"},
                    "package": {"type": "string", "maxLength": 48,
                                "description": "REQUIRED: exact physical package or pitch the request assigns to this reference, such as SOT-23, SOD-123, SOIC-8, TQFP-32, SMD 0805, or 2.54mm pitch; empty when unspecified"},
                    "polarized": {"type": "boolean",
                                  "description": "true only for explicitly polarized/electrolytic capacitors"},
                },
            },
        },
        "netlist": REQUIREMENT_SPEC["properties"]["netlist"],
    },
}
