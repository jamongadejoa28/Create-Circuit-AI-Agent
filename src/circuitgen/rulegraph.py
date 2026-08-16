"""Typed circuit-design rules compiled into the existing CircuitIR pipeline.

Unlike CircuitPattern ``apply_when`` phrases, rule selection uses normalized
requirement fields and electrical rail facts.  The LLM may classify intent,
but it does not choose coordinates, pins, nets, or claim that ERC proves a
design rule.  Every rule carries repository-backed evidence and lowers to the
well-tested pattern binder only after its typed applicability contract passes.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .netnames import is_ground, supply_voltage

RULE_DIR = Path(__file__).resolve().parents[2] / "data" / "rules"
_PROJECT = Path(__file__).resolve().parents[2]

_REQUIRED = {"id", "function", "applicability", "roles", "ports", "edges", "evidence", "status"}


def validate_rule(rule: dict) -> list[str]:
    errors: list[str] = []
    missing = _REQUIRED - set(rule)
    if missing:
        return [f"missing field {name!r}" for name in sorted(missing)]
    evidence = rule.get("evidence", {})
    if evidence.get("provenance") != "textbook":
        errors.append("evidence.provenance must be 'textbook'")
    for key in ("document", "file", "section", "pdf_page_index"):
        if evidence.get(key) in (None, ""):
            errors.append(f"evidence.{key} is required")
    source_file = evidence.get("file")
    if source_file and not (_PROJECT / source_file).is_file():
        errors.append(f"evidence.file does not exist in repository: {source_file}")

    roles = rule.get("roles", {})
    ports = rule.get("ports", {})
    endpoints = set(ports)
    for role, spec in roles.items():
        if not spec.get("component_kind"):
            errors.append(f"role {role}: component_kind is required")
        pins = spec.get("pins")
        if pins:
            endpoints.update(f"{role}.{pin}" for pin in pins)
        elif spec.get("component_kind") in {"capacitor", "resistor", "inductor", "diode"}:
            endpoints.update((f"{role}.1", f"{role}.2"))
        else:
            errors.append(f"role {role}: pins are required")
    for edge in rule.get("edges", []):
        if edge.get("relation") != "same_net":
            errors.append(f"unsupported edge relation {edge.get('relation')!r}")
        for endpoint in (edge.get("from"), edge.get("to")):
            if endpoint not in endpoints:
                errors.append(f"unknown edge endpoint {endpoint!r}")
    for pair in rule.get("constraints", {}).get("forbidden_same_net", []):
        if len(pair) != 2 or any(endpoint not in endpoints for endpoint in pair):
            errors.append(f"invalid forbidden_same_net pair {pair!r}")
    return errors


def load_rules(directory: str | Path = RULE_DIR) -> dict[str, dict]:
    rules: dict[str, dict] = {}
    for path in sorted(Path(directory).glob("*.json")):
        rule = json.loads(path.read_text(encoding="utf-8"))
        problems = validate_rule(rule)
        if problems:
            raise ValueError(f"{path.name}: " + "; ".join(problems))
        if rule["id"] in rules:
            raise ValueError(f"duplicate rule id {rule['id']!r}")
        rules[rule["id"]] = rule
    return rules


def match_rules(spec: dict, rules: dict[str, dict]) -> list[dict]:
    """Return rules whose typed requirement and rail predicates all hold."""
    kinds = Counter(
        str(part.get("functional_kind", ""))
        for part in spec.get("parts_needed", [])
        for _ in range(int(part.get("quantity", 1) or 1))
        if part.get("functional_kind")
    )
    rails = [
        rail for rail in spec.get("power", {}).get("rails", [])
        if rail.get("name") and not is_ground(str(rail["name"]))
    ]
    voltages = [
        value for rail in rails
        if (value := supply_voltage(str(rail.get("voltage") or rail["name"]))) is not None
    ]
    matches: list[dict] = []
    for rule in rules.values():
        app = rule["applicability"]
        required = app.get("required_part_kinds", {})
        if any(kinds[kind] < int(count) for kind, count in required.items()):
            continue
        if len(rails) < int(app.get("min_non_ground_rails", 0)):
            continue
        if app.get("requires_distinct_supply_voltages") and len(set(voltages)) < 2:
            continue
        forbidden = set(app.get("forbidden_part_kinds", []))
        if forbidden & set(kinds):
            continue
        matches.append(rule)
    return matches


def lower_to_pattern(rule: dict) -> dict:
    """Lower a typed rule graph to the established symbol-binding format."""
    roles: dict[str, dict] = {}
    for role, node in rule["roles"].items():
        spec = {"kind": node["component_kind"]}
        if node.get("catalog_query"):
            spec["query"] = node["catalog_query"]
        if node.get("lib_id"):
            spec["lib_id"] = node["lib_id"]
        if node.get("pins"):
            spec["pins"] = node["pins"]
        if node.get("parameter"):
            spec["param"] = node["parameter"]
        if node.get("requirement_kind"):
            spec["requirement_kind"] = node["requirement_kind"]
        if node.get("default_value"):
            spec["default_value"] = node["default_value"]
        roles[role] = spec
    return {
        "id": rule["id"],
        "function": rule["function"],
        "roles": roles,
        "ports": list(rule["ports"]),
        "topology": [[edge["from"], edge["to"]] for edge in rule["edges"]],
        "params": rule.get("parameters", {}),
        "required_wired": rule.get("required_wired", []),
        "placement": rule.get("placement", {}),
        "source": {
            "book": rule["evidence"]["document"],
            "section": rule["evidence"]["section"],
            "pdf_page_index": rule["evidence"]["pdf_page_index"],
            "tier": rule["evidence"].get("tier", "A"),
            "provenance": rule["evidence"]["provenance"],
        },
        "status": rule["status"],
        "rail_ports": {
            port: spec["rail_selector"]
            for port, spec in rule["ports"].items()
            if spec.get("rail_selector")
        },
    }


def verify_rule_instance(
    ir, rule: dict, pattern: dict, binding, refs: dict[str, str], ports: dict[str, str]
) -> list[str]:
    """Verify required graph edges plus explicit forbidden net equalities."""
    from .patterns import verify_pattern_instance

    issues = verify_pattern_instance(ir, pattern, binding, refs, ports)

    def endpoint_net(endpoint: str) -> str | None:
        if endpoint in rule["ports"]:
            return ports.get(endpoint, endpoint)
        role, pin = endpoint.split(".", 1)
        node = (refs[role], binding.pins[role][pin])
        return next((net.name for net in ir.nets if node in net.nodes), None)

    for left, right in rule.get("constraints", {}).get("forbidden_same_net", []):
        left_net, right_net = endpoint_net(left), endpoint_net(right)
        if left_net is not None and left_net == right_net:
            issues.append(f"forbidden same net {left} = {right} = {left_net}")
    return issues
