"""Footprint validation and auto-assignment.

Until now Component.footprint was free text — models wrote lib-less or
invented names and nothing caught it (KiCad's own footprint checks are
deliberately silenced in our .kicad_pro). With the official
kicad-footprints index these become deterministic:

- validation: the footprint must exist, and every pin NUMBER of the
  symbol must exist among the footprint's pad numbers (count comparison
  is not enough — KiCad connects pads by number).
- assignment: empty/unknown footprints are filled from, in order, the
  symbol's own default Footprint property, then its ki_fp_filters
  matched against the index.

Both no-op when the index carries no footprints (not built with the
kicad-footprints clone present).
"""

from __future__ import annotations

import re

from .ir import CircuitIR, SymbolDef, ValidationIssue
from .partindex import PartIndex


def requested_package_text(part: dict) -> str:
    """Combine fields that may preserve an explicit physical family."""
    return " ".join(
        str(part.get(field, "")).strip()
        for field in ("package", "search_query", "value")
        if str(part.get(field, "")).strip()
    )


def requested_footprint_constraints(requested: str) -> tuple[list[str], list[float], list[str]]:
    """Extract physical package constraints, including named families.

    `HC-49/SD` and `Pin Header` are not cosmetic prose: accepting an arbitrary
    two-pad crystal or a Tag-Connect probe footprint changes the part the user
    can mount.  A plain pin header defaults to the ubiquitous 2.54 mm family
    only when the request gives no pitch of its own.
    """
    import re

    upper = (requested or "").upper()
    tokens = re.findall(
        r"SOT[- ]?\d+|SOD[- ]?\d+|SOIC[- ]?\d+|SSOP[- ]?\d+|"
        r"TQFP[- ]?\d+|QFN[- ]?\d+|\b(?:0402|0603|0805|1206|1210)\b",
        upper,
    )
    families: list[str] = []
    if re.search(r"HC[- ]?49\s*/?[- ]?SD", upper):
        families.append("HC49SD")
    if (
        "PIN HEADER" in upper
        or "PINHEADER" in upper
        or re.search(r"\b\d+\s*[X×]\s*\d+\s+HEADER\b", upper)
    ):
        families.append("PINHEADER")
    pitches = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*MM", upper)]
    if "PINHEADER" in families and not pitches:
        pitches = [2.54]
    return tokens, pitches, families


_CONNECTOR_WORD = re.compile(r"header|connector|커넥터|헤더", re.I)
_NXM = re.compile(r"(?<!\d)(\d{1,2})\s*[xX×]\s*(\d{1,2})(?!\d)")
_NPIN = re.compile(r"(?<!\d)(\d{1,2})\s*(?:-?\s*pins?|핀)\b", re.I)


def parse_contact_geometry(text: str) -> dict | None:
    """Requested connector layout, or None when the text does not name one.

    NxM and N-pin are header notation, not circuit names. A number is geometry
    only when the same text also names a connector, so ``AMS1117-3.3`` and
    ``SOT-223`` do not become contact counts.
    """
    if not text or not _CONNECTOR_WORD.search(text):
        return None
    grid = _NXM.search(text)
    if grid:
        rows, columns = int(grid.group(1)), int(grid.group(2))
        if 1 <= rows <= 32 and 1 <= columns <= 32:
            return {"rows": rows, "columns": columns, "contacts": rows * columns}
    pins = _NPIN.search(text)
    if pins:
        contacts = int(pins.group(1))
        if 1 <= contacts <= 64:
            return {"rows": 1, "columns": contacts, "contacts": contacts}
    return None


def generic_header_lib_id(geometry: dict) -> str:
    """KiCad Connector_Generic id for an NxM pin header.

    Single-row headers are ``Conn_01xNN``. Dual-row headers have no bare
    ``Conn_02xNN`` symbol; Odd_Even is the catalog's default numbering.
    """
    rows, columns = int(geometry["rows"]), int(geometry["columns"])
    if rows == 1:
        return f"Connector_Generic:Conn_01x{columns:02d}"
    if rows == 2:
        return f"Connector_Generic:Conn_02x{columns:02d}_Odd_Even"
    return f"Connector_Generic:Conn_{rows:02d}x{columns:02d}"


def symbol_contact_count(sym: SymbolDef) -> int:
    """Distinct electrical contacts on a symbol, expanding bundled pin numbers."""
    return len(_required_pins(sym))


def _required_pins(sym: SymbolDef) -> set[str]:
    """Physical pad numbers required by a symbol.

    KiCad 10 can bundle coincident symbol pins as ``[1,15,38,39]`` while the
    footprint necessarily contains four separate pads. Comparing the bundle
    token to pad names rejected the official ESP32-WROOM-32E footprint and,
    conversely, could not prove that all thermal/ground pads were present.
    """
    out: set[str] = set()
    for pin in sym.pins:
        if pin.number.startswith("[") and pin.number.endswith("]"):
            out.update(x.strip() for x in pin.number[1:-1].split(",") if x.strip())
        else:
            out.add(pin.number)
    return out


def check_footprints(
    ir: CircuitIR, symbols: dict[str, SymbolDef], parts: PartIndex
) -> list[ValidationIssue]:
    if not parts.has_footprints():
        return []
    issues = []
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#") or not comp.footprint:
            continue
        if comp.lib_id.startswith("Conceptual:"):
            continue  # concept boxes have no physical package by definition
        pads = parts.footprint_pads(comp.footprint)
        if pads is None:
            issues.append(
                ValidationIssue(
                    "circuitgen-erc", "footprint_unknown", "error", ref,
                    f"{ref}: footprint {comp.footprint!r} does not exist in the footprint library",
                )
            )
            continue
        missing = _required_pins(sym) - pads
        if missing:
            issues.append(
                ValidationIssue(
                    "circuitgen-erc", "footprint_pin_mismatch", "error", ref,
                    f"{ref}: footprint {comp.footprint} has no pads for pins {sorted(missing)} of {comp.lib_id}",
                )
            )
            continue
        # Numbered pads absent from the symbol need review, but cannot be a
        # universal blocker: official exposed-pad footprints sometimes model
        # a manufacturing/thermal pad that their symbol deliberately omits
        # (Si7050-A20 + DFN-6-1EP is one concrete repository example).
        # The opposite direction above remains blocking: every symbol pin
        # must have a physical pad.
        extra = pads - _required_pins(sym)
        if extra:
            issues.append(
                ValidationIssue(
                    "circuitgen-erc", "footprint_pad_unbound", "warning", ref,
                    f"{ref}: footprint {comp.footprint} has electrical pads "
                    f"{sorted(extra)} absent from symbol {comp.lib_id}",
                )
            )
    return issues


def assign_footprints(
    ir: CircuitIR, symbols: dict[str, SymbolDef], parts: PartIndex,
    requested_packages: dict[str, str] | None = None,
) -> list[str]:
    """Fill or repair footprints deterministically; returns notes."""
    if not parts.has_footprints():
        return []
    notes = []
    requested_packages = requested_packages or {}
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        if comp.lib_id.startswith("Conceptual:"):
            comp.footprint = ""  # concept boxes carry no package
            continue
        required = _required_pins(sym)

        requested = requested_packages.get(ref, "")
        if comp.lib_id in {"Device:C_Polarized", "Device:C_Polarized_US"}:
            concrete, _pitches, _families = requested_footprint_constraints(requested)
            if not concrete:
                # Capacitance and a generic "SMD/electrolytic" description do
                # not determine a safe land pattern.
                if comp.footprint:
                    notes.append(
                        f"{ref}: polarized capacitor has no concrete case size; "
                        f"cleared footprint {comp.footprint!r}"
                    )
                    comp.footprint = ""
                continue
        package_tokens, requested_pitches, requested_families = (
            requested_footprint_constraints(requested)
        )
        filters = (sym.properties.get("ki_fp_filters", "") or "").split()
        if package_tokens or requested_pitches or requested_families:
            candidates = (
                parts.match_footprints(filters, required, limit=500)
                if filters else []
            )

            def requested_match(fp_id: str) -> bool:
                import re

                normalized = re.sub(r"[^A-Z0-9]", "", fp_id.upper())
                if any(re.sub(r"[^A-Z0-9]", "", token) not in normalized
                       for token in package_tokens):
                    return False
                if any(family not in normalized for family in requested_families):
                    return False
                actual_pitches = [
                    float(value) for value in re.findall(
                        r"P(\d+(?:\.\d+)?)MM", fp_id.upper()
                    )
                ]
                return not requested_pitches or any(
                    abs(want - actual) < 0.001
                    for want in requested_pitches for actual in actual_pitches
                )

            matches = [fp for fp in candidates if requested_match(fp)]
            if not matches and (package_tokens or requested_pitches or requested_families):
                # A concrete user package outranks the symbol's default
                # footprint family. KiCad's 1N4148 symbol filters only DO-35,
                # while a user may explicitly select the SMD 1N4148W in
                # SOD-123; the electrical symbol and pin numbers remain valid.
                package_filters = [
                    f"*{token.replace(' ', '-')}*" for token in package_tokens
                ]
                package_filters += [
                    f"*P{pitch:g}mm*" for pitch in requested_pitches
                ]
                package_filters += [f"*{family}*" for family in requested_families]
                matches = [
                    fp for fp in parts.match_footprints(
                        package_filters, required, limit=500
                    ) if requested_match(fp)
                ]
            if matches:
                comp.footprint = matches[0]
                notes.append(
                    f"{ref}: footprint <- {matches[0]} (requested package {requested!r})"
                )
                continue

        if comp.footprint and parts.footprint_pads(comp.footprint) is not None:
            continue  # valid as-is and no more-specific request replaced it

        default_fp = sym.properties.get("Footprint", "")
        if default_fp:
            pads = parts.footprint_pads(default_fp)
            if pads is not None and required <= pads:
                if comp.footprint != default_fp:
                    notes.append(f"{ref}: footprint <- {default_fp} (symbol default)")
                    comp.footprint = default_fp
                continue

        filters = (sym.properties.get("ki_fp_filters", "") or "").split()
        # A few generic KiCad symbols intentionally carry no filters.  Give
        # them conservative, deterministic package families rather than
        # preserving a model-invented footprint forever.
        if not filters:
            filters = {
                "Switch:SW_Push": ["SW_SPST*"],
                "Device:Fuse": ["*Fuse*"],
                "Device:D_TVS": ["D_*"],
            }.get(comp.lib_id, [])
        if filters:
            matches = parts.match_footprints(filters, required, limit=1)
            if matches:
                notes.append(f"{ref}: footprint <- {matches[0]} (fp_filters {filters})")
                comp.footprint = matches[0]
                continue

        if comp.footprint:
            # An invented footprint is worse than none: a name that does not
            # exist is a hard footprint_unknown error that blocks the build
            # forever over a PCB-layout attribute, while an absent one is a
            # footprint_missing warning the user can act on. Measured: a 7B
            # wrote 'Connector:LEMO4:LEMO4_4P' for a symbol with no default
            # footprint and no fp_filters, and an otherwise ERC-0,
            # connectivity-clean board could never pass.
            notes.append(
                f"{ref}: footprint {comp.footprint!r} does not exist and no valid "
                f"replacement was found — cleared, assign one before layout"
            )
            comp.footprint = ""
    return notes
