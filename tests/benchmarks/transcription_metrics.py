"""Exact, ground-truth-backed measurements for transcription requests.

Design quality is open-ended; transcription quality is not.  This module
compares the structured extraction with a suite fixture without consulting
part names, catalog ranking, ERC, or any other proxy for connectivity.
"""

from __future__ import annotations

from collections import Counter

from circuitgen.normalize import component_value


def expected_connections(expected: dict) -> Counter[tuple[str, str, str]]:
    return Counter(
        (str(net["name"]), str(ref).upper(), str(pin))
        for net in expected.get("netlist", [])
        for ref, pin in net.get("nodes", [])
    )


def spec_connections(spec: dict) -> Counter[tuple[str, str, str]]:
    return Counter(
        (
            str(net.get("name", "")).strip(),
            str(node.get("reference", "")).strip().upper(),
            str(node.get("pin", "")).strip(),
        )
        for net in spec.get("netlist", [])
        for node in net.get("nodes", [])
        if str(net.get("name", "")).strip()
        and str(node.get("reference", "")).strip()
        and str(node.get("pin", "")).strip()
    )


def _same_value(expected: str, actual: str) -> bool:
    wanted_number = component_value(expected)
    actual_number = component_value(actual)
    if wanted_number is not None and actual_number is not None:
        return abs(wanted_number - actual_number) <= max(abs(wanted_number), 1e-15) * 1e-9
    return "".join(expected.casefold().split()) == "".join(actual.casefold().split())


def compare_expected_spec(expected: dict, spec: dict) -> dict:
    """Return separate, inspectable transcription measurements.

    Component descriptions are intentionally not compared: "capacitor" and
    "ceramic capacitor" can bind the same requested C1. References, values,
    and every net membership are the facts the user supplied.
    """
    wanted_parts = {
        str(part.get("reference", "")).strip().upper(): part
        for part in expected.get("parts", [])
        if str(part.get("reference", "")).strip()
    }
    actual_parts = {
        str(part.get("reference", "")).strip().upper(): part
        for part in spec.get("parts_needed", [])
        if str(part.get("reference", "")).strip()
    }
    wanted_connections = expected_connections(expected)
    actual_connections = spec_connections(spec)

    missing_values: list[str] = []
    wrong_values: list[str] = []
    value_matches = 0
    values_expected = 0
    for ref, wanted in wanted_parts.items():
        wanted_value = str(wanted.get("value", "")).strip()
        if not wanted_value:
            continue
        values_expected += 1
        actual_value = str(actual_parts.get(ref, {}).get("value", "")).strip()
        if not actual_value:
            missing_values.append(ref)
        elif _same_value(wanted_value, actual_value):
            value_matches += 1
        else:
            wrong_values.append(f"{ref}: expected {wanted_value!r}, got {actual_value!r}")

    polarized_wrong: list[str] = []
    for ref, wanted in wanted_parts.items():
        if "polarized" not in wanted:
            continue
        actual = actual_parts.get(ref, {})
        if bool(wanted.get("polarized")) != bool(actual.get("polarized")):
            polarized_wrong.append(
                f"{ref}: expected polarized={wanted.get('polarized')!r}, "
                f"got {actual.get('polarized')!r}"
            )

    missing_connections = list((wanted_connections - actual_connections).elements())
    unexpected_connections = list((actual_connections - wanted_connections).elements())
    return {
        "parts_expected": len(wanted_parts),
        "parts_extracted": len(actual_parts),
        "missing_parts": sorted(set(wanted_parts) - set(actual_parts)),
        "unexpected_parts": sorted(set(actual_parts) - set(wanted_parts)),
        "parts_exact": set(wanted_parts) == set(actual_parts),
        "connections_expected": sum(wanted_connections.values()),
        "connections_extracted": sum(actual_connections.values()),
        "missing_connections": [f"{n}: {r}.{p}" for n, r, p in missing_connections],
        "unexpected_connections": [f"{n}: {r}.{p}" for n, r, p in unexpected_connections],
        "netlist_exact": wanted_connections == actual_connections,
        "values_expected": values_expected,
        "values_matched": value_matches,
        "values_missing": missing_values,
        "values_wrong": wrong_values,
        "polarized_wrong": polarized_wrong,
    }
