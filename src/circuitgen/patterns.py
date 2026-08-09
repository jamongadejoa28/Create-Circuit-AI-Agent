"""CircuitPattern: textbook circuits as parameterized, executable IR subgraphs.

The knowledge base stores short cited statements — good for prompting, but a
model cannot reuse a schematic from a sentence. A pattern stores what the
textbook example actually IS: part roles with pin capabilities, a pin-to-pin
topology, parameters with their governing formula, ports, and the citation.
`bind` resolves roles onto real KiCad symbols, `instantiate` writes the
subgraph into a CircuitIR, `verify` proves the topology landed intact.

Pattern JSON (data/patterns/*.json):
  id            unique snake_case name
  function      one-line purpose
  apply_when    lowercase keywords that trigger retrieval
  roles         role -> {kind, query?, lib_id?, param?, pins?}
                IC roles declare pins: key -> {names: [...], etype?: NAME};
                two-pin passives may omit pins (defaults to "1"/"2")
                allow_unbound_pins may be true for hub devices (MCUs) whose
                remaining pins are deliberately completed/NC'd downstream
  provides      functional subsystems this pattern implements (see
                SUBSYSTEM_KEYWORDS); a board request naming subsystems the
                pattern lacks must not be answered by it
  ports         external net names, order = naming priority (VIN, VOUT, ...)
  topology      [[endpoint, endpoint], ...]; endpoint = "ROLE.PINKEY" | port
  params        param -> formula string (documentation, not evaluated)
  required_wired  endpoint pairs that must come out as SOLID wires (consumed
                by emit/QA metrics, not by IR verification)
  placement     hints (inputs/outputs lists) for future topology placement
  source        verified citation {book, section, pdf_page_index, tier}
  status        "verified" | "draft"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ir import CircuitIR, Component, SymbolDef
from .topology import _clean

PATTERN_DIR = Path(__file__).resolve().parents[2] / "data" / "patterns"

_TWO_PIN_KINDS = {"resistor", "capacitor", "inductor", "ferrite_bead", "diode"}


@dataclass
class PatternBinding:
    """Roles resolved onto concrete KiCad symbols."""

    lib_ids: dict[str, str] = field(default_factory=dict)   # role -> lib_id
    pins: dict[str, dict[str, str]] = field(default_factory=dict)  # role -> key -> number


def _endpoint(pattern: dict, text: str):
    """('port', name) or ('pin', role, pinkey); raises ValueError if invalid."""
    if text in pattern.get("ports", []):
        return ("port", text)
    if "." in text:
        role, _, key = text.partition(".")
        pins = _role_pins(pattern, role)
        if pins is not None and key in pins:
            return ("pin", role, key)
    raise ValueError(f"invalid endpoint {text!r}")


def _role_pins(pattern: dict, role: str) -> dict | None:
    spec = pattern.get("roles", {}).get(role)
    if spec is None:
        return None
    if "pins" in spec:
        return spec["pins"]
    if spec.get("kind") in _TWO_PIN_KINDS:
        return {"1": {}, "2": {}}
    return None


def validate_pattern(pattern: dict) -> list[str]:
    errors: list[str] = []
    for key in ("id", "roles", "ports", "topology", "source", "status"):
        if key not in pattern:
            errors.append(f"missing field {key!r}")
    if errors:
        return errors
    if set(pattern["roles"]) & set(pattern["ports"]):
        errors.append("role names must not collide with port names")
    for role, spec in pattern["roles"].items():
        if _role_pins(pattern, role) is None:
            errors.append(f"role {role}: kind {spec.get('kind')!r} needs explicit pins")
        if spec.get("allow_unbound_pins") not in (None, False, True):
            errors.append(f"role {role}: allow_unbound_pins must be boolean")
        if spec.get("allow_unbound_pins") and spec.get("kind") not in {
            "microcontroller", "processor", "module", "connector"
        }:
            errors.append(
                f"role {role}: allow_unbound_pins is only valid for explicit hub roles"
            )
    seen_endpoints: set[str] = set()
    for edge in pattern["topology"]:
        if len(edge) != 2:
            errors.append(f"topology edge {edge!r} must have exactly 2 endpoints")
            continue
        for text in edge:
            try:
                _endpoint(pattern, text)
                seen_endpoints.add(text)
            except ValueError as e:
                errors.append(str(e))
    for pair in pattern.get("required_wired", []):
        for text in pair:
            if text not in seen_endpoints:
                errors.append(f"required_wired endpoint {text!r} not present in topology")
    src = pattern.get("source", {})
    for src_key in ("book", "section"):
        if not src.get(src_key):
            errors.append(f"source.{src_key} is required (no uncited patterns)")
    # A pattern derived from our own ERC-verified fixture is legitimate, but it
    # must not be indistinguishable from a textbook citation: every entry used
    # to claim "tier A" regardless of origin, which made circular authority
    # invisible to review.
    provenance = src.get("provenance")
    if provenance not in ("textbook", "internal-fixture"):
        errors.append("source.provenance must be 'textbook' or 'internal-fixture'")
    elif provenance == "textbook" and not src.get("pdf_page_index"):
        errors.append("source.pdf_page_index is required for a textbook citation")
    return errors


def load_patterns(directory: str | Path = PATTERN_DIR) -> dict[str, dict]:
    patterns: dict[str, dict] = {}
    for path in sorted(Path(directory).glob("*.json")):
        pattern = json.loads(path.read_text(encoding="utf-8"))
        problems = validate_pattern(pattern)
        if problems:
            raise ValueError(f"{path.name}: " + "; ".join(problems))
        patterns[pattern["id"]] = pattern
    return patterns


def match_patterns(text: str, patterns: dict[str, dict]) -> list[dict]:
    """Patterns whose apply_when keywords occur in the (lowercased) text.

    apply_unless keywords veto a match — needed where one trigger is a
    substring of a sibling's ("비반전 증폭" contains "반전 증폭")."""
    low = text.lower()
    return [
        p for p in patterns.values()
        if any(k.lower() in low for k in p.get("apply_when", []))
        and not any(k.lower() in low for k in p.get("apply_unless", []))
    ]


# Functional subsystems a request can ask for.  A pattern implements ONE
# function; a board request names several, and answering "Ethernet + RS485 +
# CAN-FD + SD card" with an eight-part CAN fragment is a silent scope
# failure (measured on testprompt boards 1, 6 and 11).  Keywords are matched
# against the USER PROMPT only — the extracted spec is an LLM paraphrase
# whose incidental wording would make this gate flap run to run.
SUBSYSTEM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ethernet": ("ethernet", "이더넷", "rj45", "rmii"),
    "rs485": ("rs485", "rs-485", "rs422", "rs-422", "modbus"),
    "rs232": ("rs232", "rs-232"),
    "usb": ("usb",),
    "sdcard": ("sd카드", "sd 카드", "sd card", "sdcard", "microsd", "micro sd", "tf카드"),
    "can": ("can-fd", "canfd", "can fd", "can bus", "canbus", "can 통신",
            "can통신", "can transceiver", "can 인터페이스", "can 트랜시버"),
    "i2c": ("i2c", "i²c", "iic"),
    "spi": ("spi",),
    "uart": ("uart", "시리얼 통신"),
    "wireless": ("wifi", "wi-fi", "bluetooth", "ble", "lora", "zigbee", "무선"),
    "motor": ("모터", "motor", "bldc", "foc", "stepper", "스테퍼", "servo", "서보"),
    "relay": ("relay", "릴레이"),
    "digital_io": ("digital input", "digital output", "디지털 입력", "디지털 출력",
                   "디지털 인풋", "digital i/o"),
    "analog_input": ("adc", "아날로그 입력", "analog input", "4-20ma", "0-10v"),
    "dac": ("dac",),
    "display": ("lcd", "oled", "display", "디스플레이", "tft"),
    "battery": ("battery", "배터리", "bms", "cell voltage", "셀 밸런싱", "충전기",
                "charger", "li-ion", "리튬"),
    "temperature_sensor": ("온도 센서", "온도센서", "temperature sensor",
                           "temperature monitoring", "thermistor", "서미스터", "ntc"),
    "power_supply": ("regulator", "레귤레이터", "전원부", "dc-dc", "dcdc", "buck",
                     "boost", "ldo", "smps", "power supply"),
    "debug": ("swd", "jtag", "디버그", "debug header", "디버깅"),
    "audio": ("audio", "오디오", "i2s"),
    "rtc": ("rtc", "실시간 시계"),
    "encoder": ("encoder", "엔코더"),
    "led": ("led", "발광 다이오드"),
}

# ASCII keywords need a word boundary ("can" inside "canonical", "ble" inside
# "assemble"); Korean keywords have no ASCII word characters around them.
_ASCII_KEYWORD = re.compile(r"^[a-z0-9 ./-]+$")


def requested_subsystems(text: str) -> set[str]:
    """Functional subsystems named in a request."""
    low = text.lower()
    found: set[str] = set()
    for name, keywords in SUBSYSTEM_KEYWORDS.items():
        for kw in keywords:
            if _ASCII_KEYWORD.match(kw):
                if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", low):
                    found.add(name)
                    break
            elif kw in low:
                found.add(name)
                break
    return found


def pattern_subsystems(pattern: dict) -> set[str]:
    """Subsystems a pattern implements: its declared `provides`, else inferred
    from its own trigger keywords."""
    declared = pattern.get("provides")
    if declared:
        return set(declared)
    return requested_subsystems(" ".join(pattern.get("apply_when", [])))


def out_of_scope_subsystems(text: str, pattern: dict) -> set[str]:
    """Subsystems the request names that this pattern does not implement.

    Only meaningful for multi-subsystem requests: a single-subsystem request
    that matched a pattern's keywords is the case patterns exist for, and
    demanding an exact vocabulary match there would reject working boards
    over a missing synonym.
    """
    wanted = requested_subsystems(text)
    if len(wanted) < 2:
        return set()
    return wanted - pattern_subsystems(pattern)


def bind_role_pins(pattern: dict, role: str, sym: SymbolDef) -> dict[str, str] | None:
    """Resolve one role's pin keys onto real pin NUMBERS of a symbol.

    Pin matching mirrors topology.py: cleaned-name match first, then a unique
    electrical type. Returns None if ANY pin stays unresolved — a partial
    pattern is worse than no pattern.
    """
    pin_map: dict[str, str] = {}
    used: set[str] = set()
    for key, spec in _role_pins(pattern, role).items():
        names = {n.upper() for n in spec.get("names", [])} or {key.upper()}
        hit = next(
            (p for p in sym.pins if _clean(p.name) in names and p.number not in used),
            None,
        )
        if hit is None and spec.get("etype"):
            typed = [
                p for p in sym.pins
                if p.etype.name == spec["etype"] and p.number not in used
            ]
            hit = typed[0] if len(typed) == 1 else None
        if hit is None:
            # key-as-number: passives ("1"/"2") and IEC-numbered pins with
            # blank names (relay coils A1/A2, contacts 13/14)
            hit = next(
                (p for p in sym.pins if p.number == key and p.number not in used),
                None,
            )
        if hit is None:
            return None
        pin_map[key] = hit.number
        used.add(hit.number)
    return pin_map


def bind_pattern(
    pattern: dict, role_symbols: dict[str, tuple[str, SymbolDef]]
) -> tuple[PatternBinding | None, list[str]]:
    """Resolve every role onto the given symbols; all-or-nothing."""
    binding = PatternBinding()
    errors: list[str] = []
    for role in pattern["roles"]:
        if role not in role_symbols:
            errors.append(f"role {role}: no symbol supplied")
            continue
        lib_id, sym = role_symbols[role]
        pin_map = bind_role_pins(pattern, role, sym)
        if pin_map is None:
            errors.append(f"role {role}: pins unresolved on {lib_id}")
            continue
        binding.lib_ids[role] = lib_id
        binding.pins[role] = pin_map
    return (None, errors) if errors else (binding, [])


def _net_groups(pattern: dict) -> list[list]:
    """Union-find the topology edges into net groups of parsed endpoints."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pattern["topology"]:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list] = {}
    for text in parent:
        groups.setdefault(find(text), []).append(_endpoint(pattern, text))
    return list(groups.values())


def _group_net_name(pattern: dict, group: list) -> str:
    ports = [name for kind, *rest in group if kind == "port" for name in rest]
    if ports:
        return sorted(ports, key=pattern["ports"].index)[0]
    _kind, role, key = group[0]
    return re.sub(r"[^A-Za-z0-9_+-]", "_", f"{role}_{key}").upper()


def instantiate_pattern(
    ir: CircuitIR,
    pattern: dict,
    binding: PatternBinding,
    refs: dict[str, str],
    ports: dict[str, str] | None = None,
    group: str | None = None,
    values: dict[str, str] | None = None,
) -> list[str]:
    """Write the pattern into `ir` as components + nets. Returns notes.

    `refs` maps roles to references (must be fresh); `ports` maps pattern
    ports to actual net names (unmapped ports keep their pattern name);
    `values` maps params (role spec "param") to component values.
    """
    ports = ports or {}
    values = values or {}
    notes: list[str] = []
    tile = group or pattern["id"].upper()
    for role, spec in pattern["roles"].items():
        ref = refs[role]
        if ref in ir.components:
            raise ValueError(f"ref {ref} already exists — patterns never overwrite")
        value = values.get(spec.get("param", ""), spec.get("default_value", ""))
        ir.add(Component(ref, binding.lib_ids[role], value or spec.get("kind", ""), "", tile))
        notes.append(f"pattern {pattern['id']}: {role} -> {ref} ({binding.lib_ids[role]})")
    for grp in _net_groups(pattern):
        name = _group_net_name(pattern, grp)
        net_name = ports.get(name, name)
        nodes = [
            (refs[role], binding.pins[role][key])
            for kind, *rest in grp
            if kind == "pin"
            for role, key in [rest]
        ]
        if nodes:
            ir.connect(net_name, *nodes)
    return notes


def verify_pattern_instance(
    ir: CircuitIR,
    pattern: dict,
    binding: PatternBinding,
    refs: dict[str, str],
    ports: dict[str, str] | None = None,
) -> list[str]:
    """Prove every topology edge holds in the IR: both endpoints on one net."""
    ports = ports or {}
    issues: list[str] = []

    def net_of_endpoint(text: str) -> str | None:
        parsed = _endpoint(pattern, text)
        if parsed[0] == "port":
            return ports.get(parsed[1], parsed[1])
        _kind, role, key = parsed
        node = (refs[role], binding.pins[role][key])
        return next((n.name for n in ir.nets if node in n.nodes), None)

    for a, b in pattern["topology"]:
        na, nb = net_of_endpoint(a), net_of_endpoint(b)
        if na is None or nb is None or na != nb:
            issues.append(f"edge {a} <-> {b} broken: {na!r} vs {nb!r}")
    return issues
