"""Non-executing SchGen Python to CircuitIR conversion and split clustering."""

from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from dataclasses import dataclass

from circuitgen.ir import CircuitIR, Component


class SchGenConversionError(ValueError):
    pass


_ALLOWED_CALLS = {
    "add_schematic_symbol", "add_label", "connect_pins",
    "get_pin_location", "write_out_all_wires", "append",
}
_FORBIDDEN_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.AsyncFor,
    ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try, ast.Match, ast.Lambda,
)


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return "<dynamic>"


def _literal_keywords(call: ast.Call, required: tuple[str, ...]) -> dict:
    values = {}
    for keyword in call.keywords:
        if keyword.arg in required:
            try:
                values[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, TypeError) as error:
                raise SchGenConversionError(
                    f"{_call_name(call)}.{keyword.arg} is not a literal"
                ) from error
    missing = [name for name in required if name not in values]
    if missing:
        raise SchGenConversionError(
            f"{_call_name(call)} missing {', '.join(missing)}"
        )
    return values


class _UnionFind:
    def __init__(self):
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: tuple[str, str], right: tuple[str, str]) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def schgen_code_to_ir(code: str, *, name: str) -> CircuitIR:
    """Parse only the declarative SchGen API subset; never execute code."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        raise SchGenConversionError(f"invalid Python: {error.msg}") from error
    forbidden = next((node for node in ast.walk(tree) if isinstance(node, _FORBIDDEN_NODES)), None)
    if forbidden is not None:
        raise SchGenConversionError(f"unsupported control node {type(forbidden).__name__}")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    unknown = sorted({_call_name(call) for call in calls} - _ALLOWED_CALLS)
    if unknown:
        raise SchGenConversionError(f"unsupported calls: {', '.join(unknown)}")

    ir = CircuitIR(name=name)
    labels: dict[str, str] = {}
    connections: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node)
        if call_name == "add_schematic_symbol":
            data = _literal_keywords(
                node, ("symbol_lib", "symbol_name", "reference", "value")
            )
            ref = str(data["reference"])
            component = Component(
                ref=ref,
                lib_id=f'{data["symbol_lib"]}:{data["symbol_name"]}',
                value=str(data["value"]),
            )
            if ref in ir.components and ir.components[ref] != component:
                raise SchGenConversionError(f"conflicting component reference {ref}")
            if ref not in ir.components:
                ir.add(component)
        elif call_name == "add_label":
            data = _literal_keywords(node, ("label_text", "label_ref"))
            ref, text = str(data["label_ref"]), str(data["label_text"])
            if ref in labels and labels[ref] != text:
                raise SchGenConversionError(f"conflicting label reference {ref}")
            labels[ref] = text
        elif call_name == "connect_pins":
            if len(node.args) != 4:
                raise SchGenConversionError("connect_pins requires four positional literals")
            try:
                left_ref, left_pin, right_ref, right_pin = map(ast.literal_eval, node.args)
            except (ValueError, TypeError) as error:
                raise SchGenConversionError("connect_pins arguments must be literals") from error
            connections.append(
                ((str(left_ref), str(left_pin)), (str(right_ref), str(right_pin)))
            )

    if not ir.components:
        raise SchGenConversionError("no schematic symbols")
    uf = _UnionFind()
    for left, right in connections:
        uf.union(left, right)
    groups: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for endpoint in uf.parent:
        groups[uf.find(endpoint)].add(endpoint)
    for endpoints in groups.values():
        label_names = {labels[ref] for ref, _pin in endpoints if ref in labels}
        if len(label_names) > 1:
            raise SchGenConversionError(
                "one connected component has conflicting labels: " + ", ".join(sorted(label_names))
            )
        nodes = sorted((ref, pin) for ref, pin in endpoints if ref in ir.components)
        unknown_refs = sorted(
            {ref for ref, _pin in endpoints if ref not in ir.components and ref not in labels}
        )
        if unknown_refs:
            raise SchGenConversionError(
                "connections reference undeclared endpoints: " + ", ".join(unknown_refs)
            )
        if not nodes:
            continue
        if label_names:
            net_name = next(iter(label_names))
        else:
            identity = "|".join(f"{ref}.{pin}" for ref, pin in nodes)
            net_name = "N$" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        ir.connect(net_name, *nodes)
    return ir


@dataclass
class ProjectClusters:
    split_group_by_project: dict[str, str]


def cluster_projects(rows: list[dict]) -> ProjectClusters:
    """Join projects that share prompts, code, or topology across splits.

    SchGen contains prompt variants copied between nominal source projects.
    Project-only splitting therefore leaks the same natural-language input
    into evaluation.  The connected component is the indivisible split unit;
    this keeps every observed duplicate relation on one side of the boundary.
    """
    projects = sorted({str(row["project"]) for row in rows})
    parent = {project: project for project in projects}

    def find(project: str) -> str:
        if parent[project] != project:
            parent[project] = find(parent[project])
        return parent[project]

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    by_code: dict[str, set[str]] = defaultdict(set)
    by_prompt: dict[str, set[str]] = defaultdict(set)
    by_topology: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_code[str(row["code_sha256"])].add(str(row["project"]))
        if row.get("prompt_sha256"):
            by_prompt[str(row["prompt_sha256"])].add(str(row["project"]))
        if row.get("topology_sha256"):
            by_topology[str(row["topology_sha256"])].add(str(row["project"]))
    for group in [*by_prompt.values(), *by_code.values(), *by_topology.values()]:
        ordered = sorted(group)
        for project in ordered[1:]:
            union(ordered[0], project)
    members: dict[str, list[str]] = defaultdict(list)
    for project in projects:
        members[find(project)].append(project)
    split_group = {}
    for group in members.values():
        identity = "schgen-cluster:" + hashlib.sha256("\0".join(sorted(group)).encode()).hexdigest()
        for project in group:
            split_group[project] = identity
    return ProjectClusters(split_group)
