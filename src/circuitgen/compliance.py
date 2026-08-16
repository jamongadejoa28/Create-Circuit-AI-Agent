"""Did we build what was asked for, and will it survive being powered on?

Two questions KiCad ERC cannot answer, both of which shipped as silent
failures:

1. Requirement compliance. A request for "ESP32-C3 + BME280" was answered
   with an STM32G474 + Si7050 board at ERC 0, reported as success — the
   pattern fast path pins parts by lib_id and never reads the requested
   part numbers. ERC checks wiring, not whether the named part is there.

2. Power integrity. Two bench boards passed at ERC 0 with an MCU whose
   supply pins were all marked no-connect, and with VDD tied to +5V on a
   part whose absolute maximum is 4.0 V. KiCad ERC skips NC pins and has
   no concept of a voltage rating, so both are invisible to it.

Neither check aborts a run: the schematic is still emitted, and the report
travels with the result so the caller sees exactly which requirement is
unmet. A drawn board with an honest "the MCU you asked for is missing" is
useful; a wrong board reported as done is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .erc import net_kind
from .ir import CircuitIR, SymbolDef, ValidationIssue
from .netnames import (
    STANDARD_RAILS,
    is_ground,
    is_ground_pin,
    is_supply,
    supply_voltage,
)
from .pins import PinType
from .topology import analyze_conduction

DEVICE_LIMITS_PATH = Path(__file__).resolve().parents[2] / "data" / "device_limits.json"

# Shape only: an alphanumeric token, four characters or more, containing at
# least one letter and one digit. Deliberately loose, because the CATALOG
# decides what is real (see requested_part_numbers) and shape assumptions are
# where this went wrong: requiring a letter prefix missed 1N4148, 2N3904 and
# 74HC00, and requiring two consecutive digits missed G5V-1 — all parts a user
# routinely selects. A colon is allowed so a library id (Device:LED) works too,
# since a prepared user may paste one.
_PART_TOKEN = re.compile(
    r"(?<![A-Za-z0-9:_])[A-Za-z0-9][A-Za-z0-9:_-]{2,}[A-Za-z0-9](?![A-Za-z0-9])"
)
_MIN_PART_LEN = 4



@dataclass
class ComplianceReport:
    """Verdict on "is this the circuit that was requested?"."""

    issues: list[ValidationIssue] = field(default_factory=list)
    requested_parts: list[str] = field(default_factory=list)
    satisfied_parts: list[str] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    checked_devices: list[str] = field(default_factory=list)
    role_total: int = 0
    role_present: int = 0
    role_missing: list[str] = field(default_factory=list)
    role_unverifiable: list[str] = field(default_factory=list)
    role_judged: int = 0
    role_working: int = 0
    role_not_working: list[str] = field(default_factory=list)
    dead_components: dict[str, str] = field(default_factory=dict)
    connector_geometry: list[dict] = field(default_factory=list)
    supply_rail_reach: list[dict] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "requested_parts": self.requested_parts,
            "satisfied_parts": self.satisfied_parts,
            "missing_parts": self.missing_parts,
            "voltage_checked_devices": self.checked_devices,
            "role_total": self.role_total,
            "role_present": self.role_present,
            "role_missing": self.role_missing,
            "role_unverifiable": self.role_unverifiable,
            "role_judged": self.role_judged,
            "role_working": self.role_working,
            "role_not_working": self.role_not_working,
            "dead_components": self.dead_components,
            "connector_geometry": self.connector_geometry,
            "supply_rail_reach": self.supply_rail_reach,
            "issues": [
                {"rule": i.rule, "severity": i.severity, "path": i.path, "message": i.message}
                for i in self.issues
            ],
        }


def _issue(rule: str, severity: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue("circuitgen-compliance", rule, severity, path, message)


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def requested_part_numbers(prompt: str, parts=None) -> list[str]:
    """Explicit part numbers the request names, in first-seen order.

    The PROMPT only. Reading the extracted spec as well looked reasonable —
    the extractor does move part numbers out of the prose — but measured on
    the bench it let the model's own inventions become requirements: a
    "12V to 5V regulator" prompt named no part, the 7B wrote LM2596 into a
    spec value, and that vetoed a cited LDO pattern the prompt was a perfect
    fit for. A requirement is what the user asked for.

    A token is a part number when the CATALOG says so. This used to be a
    denylist of protocol and package names (RS485, IP65, USB20...) which is
    unbounded by construction — every new standard needs another entry — and
    was written to stop specific false positives rather than from anything
    true about part numbers.
    """
    seen: dict[str, None] = {}
    for token in _PART_TOKEN.findall(prompt or ""):
        if len(token) < _MIN_PART_LEN:
            continue
        from .fp_checks import requested_footprint_constraints

        concrete_packages, _pitches, package_families = requested_footprint_constraints(token)
        normalized_token = re.sub(r"[^A-Z0-9]", "", token.upper())
        if concrete_packages and not package_families and any(
            normalized_token == re.sub(r"[^A-Z0-9]", "", package.upper())
            for package in concrete_packages
        ):
            continue  # a standardized physical package, not a BOM device
        if ":" in token:
            # A full Library:Symbol is already an exact catalog identity. Do
            # not discard it merely because the symbol suffix is short.
            if parts is not None and not parts.exact_lib_id(token):
                continue
            seen.setdefault(token, None)
            continue
        elif not (any(c.isalpha() for c in token) and any(c.isdigit() for c in token)):
            # otherwise a part number needs both letters and digits, or it is
            # an ordinary word
            continue
        if parts is not None and not any(
            part_present(token, hit["lib_id"]) for hit in parts.search_parts(token, 5)
        ):
            continue  # shaped like a part number, but no such part exists
        seen.setdefault(token, None)
    return list(seen)


def part_present(token: str, lib_id: str, value: str = "") -> bool:
    """Does this component answer a request for `token`?

    Matches the SYMBOL, tolerating the ordering-code convention KiCad uses for
    a family: a request for STM32G474RET6 is answered by STM32G474RETx.

    It deliberately does not look at `value`. That string is written by the
    pipeline, so matching it let any component satisfy the request by having
    the part number typed into it — measured on driver_relay, where
    Relay:RM50-xx21 with value "G5V-1" was reported as satisfying a request for
    G5V-1 while Relay:G5V-1 sat unused in the bundled library.

    A ``Conceptual:`` placeholder is never the requested part — measured on
    the timer campaign board, ``Conceptual:NE555D`` made selected_parts look
    present while the catalog symbol ``Timer:NE555D`` was never bound.
    """
    if (lib_id or "").startswith("Conceptual:"):
        return False
    if ":" in (token or ""):
        return token.strip().casefold() == lib_id.strip().casefold()
    want = _norm(token)
    if len(want) < 4:
        return False
    for candidate in (_norm(lib_id.split(":")[-1]),):
        if len(candidate) < 4:
            continue
        if want in candidate or candidate in want:
            return True
        # KiCad writes a trailing "x" where a family covers several ordering
        # codes (STM32G474RETx answers a request for STM32G474RET6). That
        # tolerance is for the WILDCARD only: without the x-test it also made
        # TMP101 satisfy a request for TMP100 — two different parts, silently
        # substituted, which is the failure this whole function exists to stop.
        if (
            want[:-1]
            and want[:-1] == candidate[:-1]
            and "X" in (want[-1], candidate[-1])
        ):
            return True
    return False


def check_requirements(
    ir: CircuitIR, prompt: str = "", parts=None, transcribed: bool = False
) -> tuple[list[ValidationIssue], list[str], list[str], list[str]]:
    """Every part number the request named must appear in the circuit.

    `transcribed` says the circuit was written from a net list the user
    supplied, and then a component's VALUE is theirs too — they wrote "C1:
    10uF" and the transcription copied it. Reading it is safe here for the
    same reason it is unsafe elsewhere: everywhere else the value is written
    by the pipeline, which is how Relay:RM50-xx21 labelled "G5V-1" once
    counted as a G5V-1. Without this, "10uF", "22pF", "100k", "SOT-223" and
    "2-pin" are all shaped like part numbers, the catalog has something for
    each, and three transcribed boards that were otherwise ERC-clean reported
    them as missing parts.
    """
    if transcribed:
        # The net list IS the requirement here, and `verify_transcription`
        # checks it exactly: every reference and pin the user wrote, present
        # or named as absent. Scanning the prose for part-number-SHAPED
        # tokens is the design-mode gate, and on a transcribed board it only
        # invents work — "2-pin", "SOT-223", "10uF", "100k" are a pin count,
        # a package and two values, the catalog has something for each, and
        # three otherwise ERC-clean boards reported them as missing parts.
        return [], [], [], []
    requested = requested_part_numbers(prompt, parts)
    satisfied: list[str] = []
    missing: list[str] = []
    for token in requested:
        hit = next(
            (
                ref
                for ref, comp in ir.components.items()
                if part_present(token, comp.lib_id, comp.value)
                or (transcribed and comp.value
                    and _norm(token) and _norm(token) in _norm(comp.value))
            ),
            None,
        )
        (satisfied if hit else missing).append(token)
    issues = [
        _issue(
            "requested_part_missing",
            "error",
            f"requirement:{token}",
            f"the request names {token} but no component in the circuit is one "
            f"— a substitute part is not the requested design",
        )
        for token in missing
    ]
    return issues, requested, satisfied, missing


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z0-9]+", (text or "").upper()) if len(t) > 2}


_GENERIC_ROLE_WORDS = {
    "THE", "AND", "FOR", "WITH", "PART", "PARTS", "COMPONENT", "COMPONENTS",
    "REQUIREMENT", "CONNECTION", "CIRCUIT", "MODULE",
}

def _token_hit(wanted: set[str], have: set[str]) -> bool:
    """Part numbers are single tokens that rarely match exactly: a request for
    STM32 is answered by STM32G474RETx. Substring either way, min 3 chars."""
    for w in wanted:
        for h in have:
            if len(w) >= 3 and len(h) >= 3 and (w in h or h in w):
                return True
    return False


def _role_matches(
    spec: dict, ir: CircuitIR, symbols: dict[str, SymbolDef], candidates: dict | None = None
) -> list[tuple[dict, list[str] | None]]:
    """Each requested part paired with the components answering it.

    ``None`` means unverifiable: the role text named nothing in the circuit and
    no candidate list was recorded for it, so there is no warrant for a verdict
    either way.

    One definition, used by both questions asked of a role — is it there, and
    is it doing anything — so the two can never disagree about which component
    a role refers to.

    Matching is deliberately generous: a role name is an LLM paraphrase, so a
    strict test would measure the extractor's vocabulary rather than the board.
    Being generous keeps presence honest as a FLOOR — a role reported missing
    really is missing.
    """
    candidates = candidates or {}
    physical = {
        ref: comp for ref, comp in ir.components.items()
        if not ref.startswith("#")
        and not (symbols.get(comp.lib_id) and symbols[comp.lib_id].is_power)
    }
    # the symbol only. comp.value is written by the pipeline, so reading it
    # let four electrically identical boards score 0 or 1 depending on whether
    # the string happened to contain the role's word — the same reason
    # part_present refuses it.
    comp_tokens = {
        ref: _tokens(comp.lib_id.split(":")[-1]) for ref, comp in physical.items()
    }

    out: list[tuple[dict, list[str] | None]] = []
    for part in spec.get("parts_needed", []):
        role = str(part.get("role", ""))
        reference = str(part.get("reference", "")).strip().upper()
        # In transcription mode the reference is user-authored identity, not
        # an LLM synonym. If the exact requested reference is on the board,
        # its role is present. Falling through to token similarity produced
        # role_unverifiable for J1/R1/C3 even though those exact references
        # were visibly present and already covered by verify_transcription.
        if reference:
            out.append((part, [reference] if reference in physical else []))
            continue
        query = str(part.get("search_query", "")).replace("__conceptual__", "")
        wanted = (_tokens(role) | _tokens(query)) - _GENERIC_ROLE_WORDS
        matches = [ref for ref, toks in comp_tokens.items() if _token_hit(wanted, toks)]
        offered = {h.get("lib_id") for h in candidates.get(role, []) if h.get("lib_id")}
        if not matches and offered:
            matches = [ref for ref, comp in physical.items() if comp.lib_id in offered]
        if not matches and not offered:
            out.append((part, None))
        else:
            out.append((part, matches))
    return out


def role_fulfilment(
    spec: dict, ir: CircuitIR, symbols: dict[str, SymbolDef], candidates: dict | None = None
) -> tuple[int, int, list[str], dict[str, int], list[str]]:
    """How many requested roles are represented by a real component.

    Presence only — see `role_jobs_done` for whether the component is wired so
    that it can do anything.
    """
    total = 0
    present = 0
    missing: list[str] = []
    unverifiable: list[str] = []
    shortfall: dict[str, int] = {}
    for part, matches in _role_matches(spec, ir, symbols, candidates):
        role = str(part.get("role", ""))
        total += 1
        if matches is None:
            # The synonym table that used to answer here reported the MCP6001
            # board as missing its op-amp and the STM32 board as missing its
            # MCU. An abstention is not a miss.
            unverifiable.append(role)
        elif matches:
            present += 1
            want_qty = max(1, int(part.get("quantity", 1) or 1))
            if len(matches) < want_qty:
                shortfall[role] = want_qty - len(matches)
        else:
            missing.append(role)
    return total, present, missing, shortfall, unverifiable


def role_jobs_done(
    spec: dict,
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    candidates: dict | None = None,
    dead: dict[str, str] | None = None,
) -> tuple[int, int, list[str]]:
    """Of the roles that are present, how many are wired to do their job.

    "Is the role present" and "is the role doing its job" are different
    questions, and only the second is the one the user cannot answer for
    themselves: they arrive having chosen the parts, not knowing where the
    resistor goes. Measured on driver_relay: role_fulfilment 1.0 on a board
    whose transistor collector sat on a one-pin net; measured on
    digital_control: 0.875 with compliance ok on a board where six decoupling
    capacitors and two crystals hung off a net that never reached the MCU.

    `dead` comes from `topology.analyze_conduction` — a per-component fact
    about the finished board, independent of this fuzzy role matching. A role
    counts as done when every component matched to it conducts.
    """
    dead = dead or {}
    judged = 0
    done = 0
    broken: list[str] = []
    for part, matches in _role_matches(spec, ir, symbols, candidates):
        if not matches:
            continue  # absent or unverifiable — role_fulfilment's business
        judged += 1
        stuck = [ref for ref in matches if ref in dead]
        if stuck:
            broken.append(
                f"{part.get('role', '')}: "
                + "; ".join(f"{ref} {dead[ref]}" for ref in sorted(stuck))
            )
        else:
            done += 1
    return judged, done, broken


def load_device_limits(path: str | Path = DEVICE_LIMITS_PATH) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data.get("devices", []) if d.get("match")]


def _limits_for(lib_id: str, limits: list[dict]) -> dict | None:
    up = lib_id.upper()
    return next((d for d in limits if d["match"].upper() in up), None)


def check_power_integrity(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    limits: list[dict] | None = None,
) -> tuple[list[ValidationIssue], list[str]]:
    """Every power-input pin must reach a supply, at a voltage it survives.

    The structural half needs no per-device data and applies to everything:
    a PWRIN pin that is marked no-connect or left on a signal net means the
    device is not powered, however clean the ERC report is. The voltage half
    only runs for devices whose datasheet limits are recorded in
    data/device_limits.json — silence there means unchecked, not safe.
    """
    limits = load_device_limits() if limits is None else limits
    issues: list[ValidationIssue] = []
    checked: list[str] = []

    kinds = {net.name: net_kind(ir, symbols, net) for net in ir.nets}
    pin_net: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for ref, pin_no in net.nodes:
            pin_net[(ref, str(pin_no))] = net.name
    nc = {(ref, str(pin_no)) for ref, pin_no in ir.nc_pins}

    for ref, comp in sorted(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        device = _limits_for(comp.lib_id, limits)
        if device is not None and comp.lib_id not in checked:
            checked.append(comp.lib_id)
        for pin in sym.pins:
            if pin.etype != PinType.PWRIN:
                continue
            name = (pin.name or "").upper()
            key = (ref, pin.number)
            net_name = pin_net.get(key)
            if key in nc or net_name is None:
                issues.append(
                    _issue(
                        "power_pin_unpowered",
                        "error",
                        f"{ref}.{pin.number}",
                        f"supply pin {ref}.{pin.number} ({name or 'power input'}) of "
                        f"{comp.lib_id} is not connected to any rail — an unpowered "
                        f"device passes ERC and does nothing",
                    )
                )
                continue
            if kinds.get(net_name) == "signal":
                issues.append(
                    _issue(
                        "power_pin_on_signal_net",
                        "error",
                        f"{ref}.{pin.number}",
                        f"supply pin {ref}.{pin.number} ({name or 'power input'}) of "
                        f"{comp.lib_id} sits on signal net {net_name} — no supply "
                        f"symbol or power source reaches it",
                    )
                )
                continue
            if device is None or kinds.get(net_name) == "gnd":
                continue
            prefixes = tuple(device.get("supply_pin_prefixes") or ())
            if prefixes and not name.startswith(prefixes):
                continue
            volts = supply_voltage(net_name)
            if volts is None:
                continue
            abs_max = device.get("absolute_max_v")
            op_max = device.get("operating_max_v")
            op_min = device.get("operating_min_v")
            cite = device.get("source", {}).get("document", "datasheet")
            if abs_max is not None and volts > abs_max:
                issues.append(
                    _issue(
                        "supply_over_absolute_maximum",
                        "error",
                        f"{ref}.{pin.number}",
                        f"{ref} pin {pin.number} ({name}) is on {net_name} ≈ {volts} V; "
                        f"{comp.lib_id} absolute maximum is {abs_max} V ({cite}) — "
                        f"the part is destroyed at power-up",
                    )
                )
            elif op_max is not None and volts > op_max:
                issues.append(
                    _issue(
                        "supply_outside_operating_range",
                        "warning",
                        f"{ref}.{pin.number}",
                        f"{ref} pin {pin.number} ({name}) is on {net_name} ≈ {volts} V, "
                        f"outside the {op_min}–{op_max} V operating range of "
                        f"{comp.lib_id} ({cite})",
                    )
                )
            elif op_min is not None and volts < op_min:
                issues.append(
                    _issue(
                        "supply_outside_operating_range",
                        "warning",
                        f"{ref}.{pin.number}",
                        f"{ref} pin {pin.number} ({name}) is on {net_name} ≈ {volts} V, "
                        f"below the {op_min} V minimum of {comp.lib_id} ({cite})",
                    )
                )
    return issues, checked


def ensure_device_supply_rails(
    spec: dict, ir: CircuitIR, limits: list[dict] | None = None
) -> list[str]:
    """Add a rail any device in the circuit can legally run on, if none exists.

    A pattern brings its own MCU, so the extracted spec need not mention one:
    the I2C sensor board came back with rails ``[GND]`` and the CAN board with
    ``[+5V, GND]``. Every downstream supply pass is keyed on a logic rail
    being present, so the first board left all STM32 supply pins no-connect
    and the second tied VDD to +5V — 1.0 V above the part's absolute maximum.
    Both passed KiCad ERC.

    Only devices with recorded datasheet limits are treated; the rail chosen
    is the highest standard rail inside the operating range.
    """
    limits = load_device_limits() if limits is None else limits
    rails = spec.setdefault("power", {}).setdefault("rails", [])
    notes: list[str] = []
    for lib_id in sorted({c.lib_id for c in ir.components.values()}):
        device = _limits_for(lib_id, limits)
        if device is None:
            continue
        op_min, op_max = device.get("operating_min_v"), device.get("operating_max_v")
        if op_max is None:
            continue

        def legal(volts: float | None) -> bool:
            return (
                volts is not None
                and volts <= op_max
                and (op_min is None or volts >= op_min)
            )

        if any(legal(supply_voltage(str(r.get("name", "")))) for r in rails):
            continue
        pick = next(
            ((name, label) for volts, name, label in STANDARD_RAILS if legal(volts)),
            None,
        )
        if pick is None:
            continue
        name, label = pick
        if any(str(r.get("name", "")).upper() == name for r in rails):
            continue
        rails.append({"name": name, "voltage": label})
        notes.append(
            f"added rail {name}: {lib_id} operates at {op_min}–{op_max} V and the "
            f"requirement listed no supply it can use"
        )
    return notes


def _header_like_component(comp, symbols: dict[str, SymbolDef] | None = None) -> bool:
    """Whether a placed part can satisfy a contact-geometry request.

    Library nicknames are not the electrical fact: ``Jumper:Conn_01x02`` and
    ``Connector_Generic:Conn_01x02`` have the same contacts. KiCad connector
    symbols use reference prefix J; pin-header footprints are the family the
    user can mount. A 2-terminal ``Device:R`` has neither.
    """
    fp = (comp.footprint or "").casefold().replace("-", "").replace("_", "")
    if "pinheader" in fp:
        return True
    sym = (symbols or {}).get(comp.lib_id)
    if sym is not None and (sym.reference_prefix or "").upper() == "J":
        return True
    body = comp.lib_id.split(":", 1)[-1].casefold()
    return body.startswith("conn_")


def check_connector_geometry(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    spec: dict | None = None,
    parts=None,
    prompt: str = "",
) -> tuple[list[ValidationIssue], list[dict]]:
    """Compare requested header layout to the symbol and footprint that landed.

    This is a physical contact contract, not ERC. A 1x2 request drawn as a
    4-pin symbol is a different part the user cannot mount.
    """
    from .fp_checks import parse_contact_geometry, requested_package_text, symbol_contact_count

    requests: list[tuple[str, dict]] = []
    for part in (spec or {}).get("parts_needed", []):
        text = requested_package_text(part)
        geometry = parse_contact_geometry(text)
        if geometry is None:
            continue
        ref = str(part.get("reference", "")).strip().upper()
        try:
            quantity = max(1, int(part.get("quantity", 1) or 1))
        except (TypeError, ValueError):
            quantity = 1
        for _ in range(quantity):
            requests.append((ref, geometry))
    # Geometry comes from the structured spec (including quantity). Scanning
    # the raw prompt for "header"/"connector" windows is the same shape as
    # apply_when substring matching and is not used.

    used: set[str] = set()
    records: list[dict] = []
    issues: list[ValidationIssue] = []
    leftover = [
        (ref, comp) for ref, comp in ir.components.items()
        if _header_like_component(comp, symbols) and not ref.startswith("#")
    ]

    def take_component(preferred: str):
        if preferred and preferred in ir.components and preferred not in used:
            used.add(preferred)
            return preferred, ir.components[preferred]
        for ref, comp in leftover:
            if ref in used:
                continue
            used.add(ref)
            return ref, comp
        return None, None

    for preferred, geometry in requests:
        ref, comp = take_component(preferred)
        if comp is None:
            record = {
                "reference": preferred or None,
                "requested_rows": geometry["rows"],
                "requested_columns": geometry["columns"],
                "requested_contacts": geometry["contacts"],
                "symbol_pins": None,
                "footprint_pads": None,
                "match": False,
            }
            records.append(record)
            issues.append(_issue(
                "connector_contact_geometry", "error", preferred or "connector",
                f"the request asks for a {geometry['rows']}x{geometry['columns']} "
                f"connector ({geometry['contacts']} contacts) and no connector "
                "is on the board",
            ))
            continue
        sym = symbols.get(comp.lib_id)
        symbol_pins = symbol_contact_count(sym) if sym is not None else None
        pads = None
        if parts is not None and getattr(parts, "has_footprints", lambda: False)() and comp.footprint:
            pad_set = parts.footprint_pads(comp.footprint)
            if pad_set is not None:
                pads = len(pad_set)
        match = (
            symbol_pins == geometry["contacts"]
            and (pads is None or pads == geometry["contacts"])
        )
        records.append({
            "reference": ref,
            "lib_id": comp.lib_id,
            "footprint": comp.footprint,
            "requested_rows": geometry["rows"],
            "requested_columns": geometry["columns"],
            "requested_contacts": geometry["contacts"],
            "symbol_pins": symbol_pins,
            "footprint_pads": pads,
            "match": match,
        })
        if match:
            continue
        actual = []
        if symbol_pins is not None:
            actual.append(f"{symbol_pins} symbol pins")
        if pads is not None:
            actual.append(f"{pads} footprint pads")
        issues.append(_issue(
            "connector_contact_geometry", "error", ref,
            f"{ref} was requested as {geometry['rows']}x{geometry['columns']} "
            f"({geometry['contacts']} contacts) but the board has "
            + (" and ".join(actual) or "an unbound connector")
            + " — the contact count must match before ordering",
        ))
    return issues, records


def _power_symbol_rail_names(comp) -> set[str]:
    """Names a power:* component asserts (value and/or lib_id suffix)."""
    if not (comp.lib_id or "").startswith("power:"):
        return set()
    names: set[str] = set()
    suffix = comp.lib_id.split(":", 1)[-1].strip()
    if suffix:
        names.add(suffix)
    value = (comp.value or "").strip()
    if value:
        names.add(value)
    return names


def _requested_supply_present(ir: CircuitIR, rail_name: str) -> bool:
    """True if a net or power symbol on the board matches the rail name."""
    target = rail_name.casefold()
    if any(net.name.casefold() == target for net in ir.nets):
        return True
    for comp in ir.components.values():
        if any(name.casefold() == target for name in _power_symbol_rail_names(comp)):
            return True
    return False


def _power_names_on_net(ir: CircuitIR, symbols: dict[str, SymbolDef], net) -> set[str]:
    names: set[str] = set()
    for ref, _pin_no in net.nodes:
        comp = ir.components.get(ref)
        if comp is None:
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None or not sym.is_power:
            continue
        names.update(_power_symbol_rail_names(comp))
    return names


def _looks_like_board_supply(name: str) -> bool:
    """True when an absent rail is a board defect, not extractor paraphrase.

    Error if the name is in ``STANDARD_RAILS``, or ``is_supply(name)`` and
    ``supply_voltage(name)`` is not None and >= 5.0 (covers +9V/+12V/…).
    ``STANDARD_RAILS`` already covers 1.8/3.3/5. Sub-1 V names and odd
    mid-values like ``+2V`` (LED Vf) stay warnings.
    """
    n = (name or "").strip()
    if not n:
        return False
    standard = {rail.casefold() for _, rail, _ in STANDARD_RAILS}
    standard |= {label.casefold() for _, _, label in STANDARD_RAILS}
    if n.casefold() in standard:
        return True
    if not is_supply(n):
        return False
    volts = supply_voltage(n)
    return volts is not None and volts >= 5.0


def check_requested_rail_reach(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    spec: dict | None = None,
) -> tuple[list[ValidationIssue], list[dict]]:
    """Whether each device PWRIN reaches a rail named in RequirementSpec.

    Distinct from ``check_power_integrity``: that asks "is the pin powered at
    all / at a voltage the part survives?". This asks "does the pin reach a
    *requested* supply from ``spec['power']['rails']``, and is each requested
    non-ground rail present on the board?". Empty rails means nothing to
    measure.

    Ground reach uses ``is_ground_pin`` only — ``V-``/``VEE`` are supply pins
    per ``netnames.is_supply_pin`` and need a device rule, not a name list.
    """
    raw = (spec or {}).get("power", {}).get("rails") or []
    if not raw:
        return [], []

    requested: list[dict] = []
    for rail in raw:
        name = str(rail.get("name", "")).strip()
        if not name:
            continue
        requested.append({"name": name, "voltage": rail.get("voltage")})
    if not requested:
        return [], []

    supply_rails = [r["name"] for r in requested if not is_ground(r["name"])]
    ground_rails = [r["name"] for r in requested if is_ground(r["name"])]
    supply_cf = {n.casefold() for n in supply_rails}
    ground_cf = {n.casefold() for n in ground_rails}

    pin_net: dict[tuple[str, str], str] = {}
    nets_by_name = {net.name: net for net in ir.nets}
    for net in ir.nets:
        for ref, pin_no in net.nodes:
            pin_net[(ref, str(pin_no))] = net.name
    nc = {(ref, str(pin_no)) for ref, pin_no in ir.nc_pins}
    kinds = {net.name: net_kind(ir, symbols, net) for net in ir.nets}

    issues: list[ValidationIssue] = []
    for rail_name in supply_rails:
        if _requested_supply_present(ir, rail_name):
            continue
        if _looks_like_board_supply(rail_name):
            issues.append(_issue(
                "requested_rail_absent",
                "error",
                f"rail:{rail_name}",
                f"the requirement asks for supply rail {rail_name!r} but no net or "
                f"power symbol of that name is on the board",
            ))
        else:
            issues.append(_issue(
                "requested_rail_absent",
                "warning",
                f"rail:{rail_name}",
                f"extracted rail {rail_name!r} may not be a board supply "
                f"(extractor paraphrase); no net or power symbol of that name "
                f"is on the board",
            ))

    records: list[dict] = []
    for ref, comp in sorted(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        for pin in sym.pins:
            if pin.etype != PinType.PWRIN:
                continue
            key = (ref, pin.number)
            net_name = pin_net.get(key)
            pin_label = pin.name or ""
            record = {
                "reference": ref,
                "pin": pin.number,
                "pin_name": pin_label,
                "lib_id": comp.lib_id,
                "net": net_name,
                "requested_rails": list(supply_rails),
                "match": False,
                "reason": "unconnected",
            }
            if key in nc or net_name is None:
                records.append(record)
                continue

            net = nets_by_name.get(net_name)
            power_on_net = (
                {n.casefold() for n in _power_names_on_net(ir, symbols, net)}
                if net is not None else set()
            )
            net_cf = net_name.casefold()
            reaches_supply = net_cf in supply_cf or bool(power_on_net & supply_cf)
            reaches_ground = (
                net_cf in ground_cf or bool(power_on_net & ground_cf)
                or kinds.get(net_name) == "gnd"
            )
            if is_ground_pin(pin_label):
                on_requested = reaches_ground
            else:
                on_requested = reaches_supply
            if on_requested:
                record["match"] = True
                record["reason"] = "reaches_requested_rail"
                records.append(record)
                continue

            if kinds.get(net_name) == "signal":
                record["reason"] = "signal_or_other"
                records.append(record)
                # Existing check_power_integrity already flags signal PWRIN;
                # do not double-fire a second error rule for the same pin.
                continue

            record["reason"] = "not_requested_rail"
            records.append(record)
            if is_ground_pin(pin_label):
                expected = ground_rails + supply_rails
                if not expected:
                    continue
                rail_list = ", ".join(expected)
                hint = (
                    f"requested ground rails ({rail_list})"
                    if ground_rails
                    else f"requested rails ({rail_list})"
                )
            else:
                if not supply_rails:
                    continue
                hint = f"requested rail ({', '.join(supply_rails)})"
            issues.append(_issue(
                "power_pin_misses_requested_rail",
                "error",
                f"{ref}.{pin.number}",
                f"supply pin {ref}.{pin.number} ({pin_label or 'power input'}) of "
                f"{comp.lib_id} is on {net_name}, which is not any {hint}",
            ))

    has_conceptual = any(
        not ref.startswith("#")
        and (symbols.get(comp.lib_id) is None or not symbols[comp.lib_id].is_power)
        and comp.lib_id.startswith("Conceptual:")
        for ref, comp in ir.components.items()
    )
    if has_conceptual and not records and supply_rails:
        issues.append(_issue(
            "supply_rail_reach_unverifiable",
            "warning",
            "supply_rail_reach",
            "conceptual placeholders have no catalog PWRIN pins, so requested "
            "rail reach cannot be measured",
        ))
    return issues, records


def check_compliance(
    ir: CircuitIR,
    symbols: dict[str, SymbolDef],
    prompt: str = "",
    parts=None,
    spec: dict | None = None,
    candidates: dict | None = None,
    transcribed: bool = False,
) -> ComplianceReport:
    """Requirement compliance + power integrity over the finished circuit."""
    req_issues, requested, satisfied, missing = check_requirements(
        ir, prompt, parts, transcribed=transcribed
    )
    pwr_issues, checked = check_power_integrity(ir, symbols)
    rail_issues, rail_records = check_requested_rail_reach(ir, symbols, spec)

    # A number/name pair in a transcribed net list is a physical binding
    # assertion.  Check it against device-local, provenance-backed data; a
    # coincident pin number on a different function is not a valid match.
    binding_issues: list[ValidationIssue] = []
    if transcribed:
        from .devicebindings import device_pin_names_compatible

        for net in (spec or {}).get("netlist", []):
            for node in net.get("nodes", []):
                ref = str(node.get("reference", "")).strip().upper()
                pin = str(node.get("pin", "")).strip()
                requested_name = str(node.get("pin_name", "")).strip()
                comp = ir.components.get(ref)
                if not comp or not pin or not requested_name:
                    continue
                sym = symbols.get(comp.lib_id)
                try:
                    catalog_name = sym.pin(pin).name if sym else ""
                except KeyError:
                    catalog_name = ""
                verdict = device_pin_names_compatible(
                    comp.lib_id, pin, requested_name, catalog_name
                )
                if verdict is False:
                    binding_issues.append(_issue(
                        "canonical_pin_binding_conflict", "error", f"{ref}.{pin}",
                        f"the request names {ref} pin {pin} as {requested_name!r}, "
                        f"but provenance-backed {comp.lib_id} defines it as "
                        f"{catalog_name or 'another function'!r}",
                    ))

    # A role the requirement asked for and the board does not contain means the
    # board does not answer the request — whatever its ERC score. This replaces
    # a 109-keyword subsystem vocabulary that existed to stop three specific
    # board prompts from being answered by an eight-part fragment: the fragment
    # is not wrong because of what the prompt SAYS, it is wrong because most of
    # what was asked for is absent, and that is measurable on the finished board.
    role_total, role_present, role_missing, shortfall, role_unverifiable = role_fulfilment(
        spec or {}, ir, symbols, candidates
    )
    # WARNING, not error: parts_needed is the extractor's paraphrase and
    # contains its inventions — a "12V to 5V regulator" prompt that names no
    # resistor still produced a `resistor` role. Failing the board for that
    # would make a model invention a requirement, which is the rule already
    # settled for part numbers. What the USER named is checked above, from the
    # prompt; this is a loud report on what the pipeline dropped.
    role_issues = [
        _issue(
            "requested_role_missing", "warning", f"requirement:{role}",
            f"the requirement asks for {role!r} and no component in the circuit "
            f"answers it",
        )
        for role in role_missing
    ] + [
        _issue(
            "role_unverifiable", "warning", f"requirement:{role}",
            f"cannot tell whether {role!r} is on the board: nothing in the "
            f"circuit names it and no candidate list was recorded for it",
        )
        for role in role_unverifiable
    ] + [
        _issue(
            "requested_quantity_short", "warning", f"requirement:{role}",
            f"{role!r} is present but {short} short of the requested quantity",
        )
        for role, short in sorted(shortfall.items())
    ]

    package_issues: list[ValidationIssue] = []
    from .fp_checks import requested_footprint_constraints, requested_package_text

    for part in (spec or {}).get("parts_needed", []):
        ref = str(part.get("reference", "")).strip().upper()
        requested_package = requested_package_text(part)
        comp = ir.components.get(ref)
        if not ref or not requested_package or comp is None:
            continue
        concrete, pitch_values, families = requested_footprint_constraints(requested_package)
        pitches = [str(value) for value in pitch_values]
        if not concrete and not pitches and not families:
            continue
        footprint_upper = comp.footprint.upper()
        normalized_fp = re.sub(r"[^A-Z0-9]", "", footprint_upper)
        missing_tokens = [
            token for token in concrete
            if re.sub(r"[^A-Z0-9]", "", token) not in normalized_fp
        ]
        missing_families = [family for family in families if family not in normalized_fp]
        footprint_pitches = [
            float(value) for value in re.findall(r"P(\d+(?:\.\d+)?)MM", footprint_upper)
        ]
        pitch_ok = not pitches or any(
            abs(float(pitch) - actual) < 0.001
            for pitch in pitches for actual in footprint_pitches
        )
        if not comp.footprint:
            message = (
                f"{ref} requests package {requested_package!r}, but no footprint "
                "was assigned"
            )
        elif missing_tokens or missing_families or not pitch_ok:
            message = (
                f"{ref} requests package {requested_package!r}, but footprint "
                f"{comp.footprint!r} does not match it"
            )
        else:
            continue
        package_issues.append(_issue(
            "requested_package_mismatch", "error", ref,
            message + " — choose a matching footprint before ordering",
        ))

    conceptual_issues = [
        _issue(
            "conceptual_part_unresolved", "error", ref,
            f"{ref} is only a conceptual placeholder for {comp.value or comp.lib_id.split(':', 1)[-1]!r}; "
            "bind the requested catalog symbol and a matching footprint before ordering",
        )
        for ref, comp in ir.components.items()
        if not ref.startswith("#") and comp.lib_id.startswith("Conceptual:")
    ]

    # Presence was never the question the user needed answered. They arrive
    # with the parts chosen and cannot tell where the resistor goes, so a board
    # can hold every part they named and still be one they cannot order:
    # measured on driver_relay, role_fulfilment 1.0 with the transistor
    # collector on a one-pin net; on digital_control, compliance ok with six
    # decoupling capacitors and two crystals on a net that never reached the
    # MCU. Conduction is a fact about the finished board — a pin that reaches
    # nothing, a part shorted across one net, two ends at the same potential —
    # so unlike the role paraphrase above it is reported as an ERROR.
    conduction = analyze_conduction(ir, symbols, every_pin=not transcribed)
    role_judged, role_working, role_broken = role_jobs_done(
        spec or {}, ir, symbols, candidates, conduction.dead
    )
    dead_issues = [
        _issue(
            "component_does_no_work", "error", ref,
            f"{ref} ({ir.components[ref].lib_id}) is on the board but can carry "
            f"no current: {why}",
        )
        for ref, why in sorted(conduction.dead.items())
    ]

    geometry_issues, geometry_records = check_connector_geometry(
        ir, symbols, spec, parts, prompt
    )

    return ComplianceReport(
        issues=(req_issues + pwr_issues + rail_issues + binding_issues + role_issues
                + package_issues + conceptual_issues + dead_issues + geometry_issues),
        requested_parts=requested,
        satisfied_parts=satisfied,
        missing_parts=missing,
        checked_devices=checked,
        role_total=role_total,
        role_present=role_present,
        role_missing=role_missing,
        role_unverifiable=role_unverifiable,
        role_judged=role_judged,
        role_working=role_working,
        role_not_working=role_broken,
        dead_components=conduction.dead,
        connector_geometry=geometry_records,
        supply_rail_reach=rail_records,
    )
