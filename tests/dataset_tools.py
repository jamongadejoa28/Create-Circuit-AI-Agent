"""Research-only dataset envelope, adapters and audit helpers.

This module deliberately lives under ``tests/``.  Public corpora are inputs
to experiments, not runtime design knowledge, and must never silently affect
the service in ``src/circuitgen``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Iterable

from pathlib import Path

from simp_sexp import Sexp

from circuitgen.ir import CircuitIR, Component
from circuitgen.ir_json import ir_to_json


SCHEMA_VERSION = "dataset-example-v1"
MODES = {"transcription", "design"}
REVIEW_STATES = {"candidate", "accepted", "rejected"}
SPLITS = {"train", "validation", "test"}


def _text(value) -> str:
    """Decode strings that dataset-server exposes as JSON string literals."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else value
        except json.JSONDecodeError:
            pass
    return value


def stable_split(source_project: str) -> str:
    """Repository-level 80/10/10 split, stable across machines and runs."""
    bucket = int(hashlib.sha256(source_project.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def canonical_ir(ir: dict | CircuitIR | None) -> dict | None:
    """Order-independent CircuitIR form used for duplicate detection."""
    if ir is None:
        return None
    data = ir_to_json(ir) if isinstance(ir, CircuitIR) else deepcopy(ir)
    components = sorted(
        (
            str(c.get("ref", "")), str(c.get("lib_id", "")),
            str(c.get("value", "")), str(c.get("footprint", "")),
        )
        for c in data.get("components", [])
        if not str(c.get("ref", "")).startswith("#")
    )
    nets = sorted(
        (
            str(net.get("name", "")),
            tuple(sorted(
                (str(node.get("ref", "")), str(node.get("pin", "")))
                for node in net.get("nodes", [])
                if not str(node.get("ref", "")).startswith("#")
            )),
        )
        for net in data.get("nets", [])
    )
    nc_pins = sorted(
        (str(node.get("ref", "")), str(node.get("pin", "")))
        for node in data.get("nc_pins", [])
        if not str(node.get("ref", "")).startswith("#")
    )
    return {
        "name": str(data.get("name", "")),
        "components": components,
        "nets": nets,
        "nc_pins": nc_pins,
    }

def circuit_fingerprint(ir: dict | CircuitIR | None) -> str | None:
    canonical = canonical_ir(ir)
    if canonical is None:
        return None
    # Names are presentation, not topology identity.
    canonical["name"] = ""
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ir_from_kicad_netlist(path: str | Path, *, name: str | None = None) -> CircuitIR:
    """Convert a KiCad-exported netlist into the compact runtime CircuitIR.

    This intentionally consumes KiCad's exported connectivity oracle rather
    than reverse-engineering drawing coordinates from ``.kicad_sch``. Relative
    placement is mined separately from reviewed drawings.
    """
    path = Path(path)
    sx = Sexp(path.read_text(encoding="utf-8"))
    ir = CircuitIR(name=name or path.stem)

    def direct(node, key: str):
        for item in node:
            if isinstance(item, list) and item and item[0] == key:
                return str(item[1]) if len(item) > 1 else ""
        return ""

    for comp in sx.search("/export/components/comp"):
        ref = direct(comp, "ref")
        if not ref or ref.startswith("#"):
            continue
        lib_id = ""
        for item in comp:
            if isinstance(item, list) and item and item[0] == "libsource":
                library, part = direct(item, "lib"), direct(item, "part")
                lib_id = f"{library}:{part}" if library and part else part or library
                break
        ir.add(Component(
            ref=ref, lib_id=lib_id, value=direct(comp, "value"),
            footprint=direct(comp, "footprint"),
        ))

    for net in sx.search("/export/nets/net"):
        net_name = direct(net, "name")
        nodes: list[tuple[str, str]] = []
        for item in net:
            if not (isinstance(item, list) and item and item[0] == "node"):
                continue
            ref, pin = direct(item, "ref"), direct(item, "pin")
            if ref in ir.components and pin:
                nodes.append((ref, pin))
        if not nodes:
            continue
        if net_name.startswith("unconnected-") and len(nodes) == 1:
            ir.nc_pins.append(nodes[0])
        else:
            ir.connect(net_name, *nodes)
    return ir


def validate_example(example: dict) -> list[str]:
    """Validate the decision-bearing subset of DatasetExample v1.

    The checked-in JSON Schema documents interchange shape.  This validator
    keeps the audit CLI dependency-free and adds cross-field rules that JSON
    Schema cannot express clearly.
    """
    errors: list[str] = []
    if example.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    provenance = example.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance is required")
        provenance = {}
    for field in ("dataset", "source_project", "license", "source_revision"):
        if not str(provenance.get(field, "")).strip():
            errors.append(f"provenance.{field} is required")
    input_data = example.get("input")
    if not isinstance(input_data, dict):
        errors.append("input is required")
        input_data = {}
    if input_data.get("mode") not in MODES:
        errors.append("input.mode must be transcription or design")
    if not str(input_data.get("prompt", "")).strip():
        errors.append("input.prompt is required")
    declared_split = example.get("split")
    if declared_split not in SPLITS:
        errors.append("split must be train, validation or test")
    split_group = provenance.get("split_group") or provenance.get("source_project")
    if declared_split in SPLITS and split_group and declared_split != stable_split(split_group):
        errors.append("split does not match repository-level stable split")

    expected = example.get("expected")
    if not isinstance(expected, dict):
        errors.append("expected is required")
        expected = {}
    ir = expected.get("canonical_ir")
    if ir is not None:
        refs = [str(c.get("ref", "")) for c in ir.get("components", [])]
        if not refs or any(not ref for ref in refs):
            errors.append("expected.canonical_ir must contain referenced components")
        if len(refs) != len(set(refs)):
            errors.append("expected.canonical_ir has duplicate component references")
        ref_set = set(refs)
        for net in ir.get("nets", []):
            for node in net.get("nodes", []):
                if node.get("ref") not in ref_set:
                    errors.append(f"net {net.get('name', '')!r} references unknown component {node.get('ref')!r}")

    validation = example.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation is required")
        validation = {}
    state = validation.get("review_status")
    if state not in REVIEW_STATES:
        errors.append("validation.review_status must be candidate, accepted or rejected")
    required_checks = (
        "parse_ok", "symbol_binding_ok", "netlist_round_trip_ok", "render_ok",
    )
    for check in required_checks:
        if not isinstance(validation.get(check), bool):
            errors.append(f"validation.{check} must be boolean")
    if state == "accepted":
        if ir is None:
            errors.append("accepted example requires expected.canonical_ir")
        for check in required_checks:
            if validation.get(check) is not True:
                errors.append(f"accepted example requires validation.{check}=true")
        if validation.get("known_issues"):
            errors.append("accepted example cannot retain known_issues")
    return errors


def audit_examples(examples: Iterable[dict]) -> dict:
    """Return inspectable errors, duplicates and repository split leakage."""
    rows = list(examples)
    errors: dict[str, list[str]] = {}
    fingerprints: dict[str, list[str]] = defaultdict(list)
    projects: dict[str, set[str]] = defaultdict(set)
    topology_splits: dict[str, set[str]] = defaultdict(set)
    states = Counter()
    issue_counts = Counter()
    external_fingerprints: dict[str, list[str]] = defaultdict(list)
    for index, example in enumerate(rows):
        example_id = str(example.get("id") or f"row-{index}")
        problems = validate_example(example)
        if problems:
            errors[example_id] = problems
        state = str(example.get("validation", {}).get("review_status", "invalid"))
        states[state] += 1
        for issue in example.get("validation", {}).get("known_issues", []):
            issue_counts[str(issue)] += 1
        fingerprint = circuit_fingerprint(example.get("expected", {}).get("canonical_ir"))
        if fingerprint:
            fingerprints[fingerprint].append(example_id)
            topology_splits[fingerprint].add(str(example.get("split", "")))
        external_hash = str(
            example.get("expected", {}).get("external_representation", {}).get("sha256", "")
        )
        if external_hash:
            external_fingerprints[external_hash].append(example_id)
        project = str(example.get("provenance", {}).get("source_project", ""))
        split = str(example.get("split", ""))
        if project and split:
            projects[project].add(split)
    duplicates = [ids for ids in fingerprints.values() if len(ids) > 1]
    external_duplicates = [
        ids for ids in external_fingerprints.values() if len(ids) > 1
    ]
    leakage = {project: sorted(splits) for project, splits in projects.items() if len(splits) > 1}
    topology_leakage = {
        fingerprint: sorted(splits)
        for fingerprint, splits in topology_splits.items()
        if len(splits) > 1
    }
    accepted = [
        str(row.get("id")) for row in rows
        if row.get("validation", {}).get("review_status") == "accepted"
        and str(row.get("id")) not in errors
        and not any(str(row.get("id")) in group for group in duplicates)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "examples": len(rows),
        "states": dict(sorted(states.items())),
        "errors": errors,
        "duplicates": duplicates,
        "external_duplicates": external_duplicates,
        "split_leakage": leakage,
        "topology_split_leakage": topology_leakage,
        # A schema-clean candidate is still quarantined.  Keep the actual
        # reasons visible so a 100-row fetch cannot be mistaken for 100 usable
        # training examples.
        "known_issue_counts": dict(sorted(issue_counts.items())),
        "quarantined": sum(
            1 for row in rows
            if row.get("validation", {}).get("review_status") == "candidate"
        ),
        "accepted_ids": accepted,
    }


def example_from_ir(
    *, example_id: str, prompt: str, mode: str, ir: CircuitIR,
    dataset: str, source_project: str, license_id: str, source_revision: str,
    requirements: dict | None = None, physical_bindings: list | None = None,
    design_rules: list | None = None, placement_constraints: list | None = None,
    validation: dict | None = None,
) -> dict:
    """Wrap a locally verified CircuitIR as DatasetExample v1."""
    checks = {
        "review_status": "candidate",
        "parse_ok": False,
        "symbol_binding_ok": False,
        "netlist_round_trip_ok": False,
        "render_ok": False,
        "known_issues": [],
        **(validation or {}),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "id": example_id,
        "split": stable_split(source_project),
        "provenance": {
            "dataset": dataset,
            "source_project": source_project,
            "license": license_id,
            "source_revision": source_revision,
            "extraction_tool": "circuitgen",
            "kicad_version": "10.x",
        },
        "input": {"prompt": prompt, "mode": mode},
        "requirements": requirements or {},
        "expected": {
            "canonical_ir": ir_to_json(ir),
            "physical_bindings": physical_bindings or [],
            "design_rules": design_rules or [],
            "relative_placement_constraints": placement_constraints or [],
        },
        "validation": checks,
    }


def adapt_schgen_row(row: dict, *, revision: str = "unknown") -> dict:
    """Create a quarantined candidate from a SchGen dataset-server row."""
    messages = row.get("messages") or []
    prompt = next((_text(m.get("content")) for m in messages if m.get("role") == "user"), "")
    code = next((_text(m.get("content")) for m in messages if m.get("role") == "assistant"), "")
    meta = row.get("meta") or {}
    project = _text(meta.get("module")) or _text(meta.get("schematic")) or "unknown"
    example_id = "schgen-" + hashlib.sha256((project + prompt).encode()).hexdigest()[:16]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": example_id,
        "split": stable_split(project),
        "provenance": {
            "dataset": "microsoft/SchGen_dataset",
            "source_project": project,
            "license": "MIT-dataset; verify-upstream-design-license",
            "source_revision": revision,
            "extraction_tool": "schgen-dataset-adapter",
            "kicad_version": "8.x",
        },
        "input": {"prompt": prompt, "mode": "design"},
        "requirements": {},
        "expected": {
            "canonical_ir": None,
            "physical_bindings": [], "design_rules": [],
            "relative_placement_constraints": [],
            "external_representation": {
                "kind": "schgen-python", "sha256": hashlib.sha256(code.encode()).hexdigest(),
                "bytes": len(code.encode("utf-8")),
                "schematic": _text(meta.get("schematic")),
                "style": _text(meta.get("style")),
            },
        },
        "validation": {
            "review_status": "candidate", "parse_ok": False,
            "symbol_binding_ok": False, "netlist_round_trip_ok": False,
            "render_ok": False,
            "known_issues": ["requires sandboxed code conversion", "upstream project license unverified"],
        },
    }
