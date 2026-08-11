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
from .netnames import STANDARD_RAILS, supply_voltage
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
        if ":" in token:
            # a library id the user pasted; the catalog indexes the symbol name
            token = token.split(":")[-1]
            if len(token) < _MIN_PART_LEN:
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
    """
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

    return ComplianceReport(
        issues=req_issues + pwr_issues + role_issues + dead_issues,
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
    )
