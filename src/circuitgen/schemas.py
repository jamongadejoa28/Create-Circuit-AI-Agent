"""JSON Schemas for every LLM interaction (plan §7.3).

Every model reply is forced through one of these via llama-server's
json_schema constrained decoding — malformed structure is impossible by
construction; content errors (unknown parts/pins) are caught by the
deterministic validators afterward.

Kept deliberately small: these schemas travel in every request, and the
context budget is ~8k tokens (plan §4).
"""

REQUIREMENT_SPEC = {
    "type": "object",
    "required": ["summary", "power", "parts_needed", "connections_intent"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "one-line normalized requirement"},
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
                            "voltage": {"type": "string"},
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
                    "search_query": {"type": "string", "description": "part-index search terms, English"},
                    "value": {"type": "string", "description": "component value if applicable, e.g. 330R"},
                },
            },
        },
        "connections_intent": {
            "type": "array",
            "items": {"type": "string"},
            "description": "plain statements like 'btn1 between +5V and led1 anode via resistor'",
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {
            "type": "boolean",
            "description": "true if the request exceeds 24VDC/3A, mains, isolation, or safety-critical scope",
        },
        "out_of_scope_reason": {"type": "string"},
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
                    "ref": {"type": "string", "pattern": "^#?[A-Za-z]+[0-9]+$"},
                    "lib_id": {"type": "string", "description": "EXACT id from the candidate list"},
                    "value": {"type": "string"},
                    "footprint": {"type": "string"},
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
        "analysis": {"type": "string", "description": "one sentence on the root cause"},
        "ops": {"type": "array", "maxItems": 12, "items": {"anyOf": _OP_VARIANTS}},
    },
}
