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
from .netnames import supply_voltage
from .pins import PinType

DEVICE_LIMITS_PATH = Path(__file__).resolve().parents[2] / "data" / "device_limits.json"

# A part number: a short letter prefix, at least two digits, then optional
# suffix — BME280, SHT30, LM358, RP2040, ESP32-C3, STM32G474RET6.  Five
# characters minimum, so pin names (PA15), bus indices (I2C1, USART1) and
# bare numbers (24V, 0805) cannot qualify.  agent._ensure_named_parts uses a
# stricter regex that misses BME280 entirely; this one is the reference.
_PART_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{1,6}[0-9]{2,}(?:[A-Za-z0-9-]*[A-Za-z0-9])?(?![A-Za-z0-9])"
)
_MIN_PART_LEN = 5

# Tokens shaped like part numbers that name a protocol, package or rating.
# Compared after normalization, so "RS-485" and "RS485" are the same entry.
_NOT_A_PART = {
    "RS485", "RS232", "RS422", "RS423", "CANFD", "MODBUS",
    "IP20", "IP54", "IP65", "IP67", "USB20", "USB30", "USB31",
    "IEC61131", "ISO11898", "IEEE8023", "80211",
}


@dataclass
class ComplianceReport:
    """Verdict on "is this the circuit that was requested?"."""

    issues: list[ValidationIssue] = field(default_factory=list)
    requested_parts: list[str] = field(default_factory=list)
    satisfied_parts: list[str] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    checked_devices: list[str] = field(default_factory=list)

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
            "issues": [
                {"rule": i.rule, "severity": i.severity, "path": i.path, "message": i.message}
                for i in self.issues
            ],
        }


def _issue(rule: str, severity: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue("circuitgen-compliance", rule, severity, path, message)


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def requested_part_numbers(prompt: str) -> list[str]:
    """Explicit part numbers the request names, in first-seen order.

    The PROMPT only. Reading the extracted spec as well looked reasonable —
    the extractor does move part numbers out of the prose — but measured on
    the bench it let the model's own inventions become requirements: a
    "12V to 5V regulator" prompt named no part, the 7B wrote LM2596 into a
    spec value, and that vetoed a cited LDO pattern the prompt was a perfect
    fit for. A requirement is what the user asked for.
    """
    seen: dict[str, None] = {}
    for token in _PART_TOKEN.findall(prompt or ""):
        if len(token) < _MIN_PART_LEN or _norm(token) in _NOT_A_PART:
            continue
        seen.setdefault(token, None)
    return list(seen)


def part_present(token: str, lib_id: str, value: str = "") -> bool:
    """Does this component answer a request for `token`?

    Matches the symbol name or the value, tolerating the ordering-code
    convention KiCad libraries use for a whole family: a request for
    STM32G474RET6 is satisfied by the symbol STM32G474RETx.
    """
    want = _norm(token)
    if len(want) < 4:
        return False
    for candidate in (_norm(lib_id.split(":")[-1]), _norm(value)):
        if len(candidate) < 4:
            continue
        if want in candidate or candidate in want:
            return True
        if want[:-1] and want[:-1] == candidate[:-1]:
            return True  # ...RET6 vs ...RETx
    return False


def check_requirements(
    ir: CircuitIR, prompt: str = ""
) -> tuple[list[ValidationIssue], list[str], list[str], list[str]]:
    """Every part number the request named must appear in the circuit."""
    requested = requested_part_numbers(prompt)
    satisfied: list[str] = []
    missing: list[str] = []
    for token in requested:
        hit = next(
            (
                ref
                for ref, comp in ir.components.items()
                if part_present(token, comp.lib_id, comp.value)
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


# Rails the pipeline knows how to create supply symbols for, highest first.
_STANDARD_RAILS = ((5.0, "+5V", "5V"), (3.3, "+3V3", "3.3V"), (1.8, "+1V8", "1.8V"))


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
            ((name, label) for volts, name, label in _STANDARD_RAILS if legal(volts)),
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
) -> ComplianceReport:
    """Requirement compliance + power integrity over the finished circuit."""
    req_issues, requested, satisfied, missing = check_requirements(ir, prompt)
    pwr_issues, checked = check_power_integrity(ir, symbols)
    return ComplianceReport(
        issues=req_issues + pwr_issues,
        requested_parts=requested,
        satisfied_parts=satisfied,
        missing_parts=missing,
        checked_devices=checked,
    )
