"""KiCad .kicad_sym library parser.

Parsing approach follows SKiDL's tools/kicad10/lib.py (MIT License,
Copyright (c) Dave Vandenbout) — s-expression traversal via simp_sexp —
but reimplemented against our own SymbolDef/PinDef model, and it also
keeps the verbatim source text of each symbol so the emitter can embed it
into a schematic's lib_symbols block unchanged.

Handles `extends` inheritance (derived symbol reuses the parent's pins and
graphics under its own name).
"""

from __future__ import annotations

import re
from pathlib import Path

from simp_sexp import Sexp

from .ir import PinDef, SymbolDef
from .pins import KICAD_PIN_TYPES

KICAD_SYMBOL_DIR = Path("/mnt/c/Program Files/KiCad/10.0/share/kicad/symbols")


def _extract_toplevel_blocks(text: str) -> dict[str, str]:
    """Cut verbatim depth-1 `(symbol "NAME" ...)` blocks out of library text."""
    blocks: dict[str, str] = {}
    for m in re.finditer(r'\(symbol\s+"((?:[^"\\]|\\.)*)"', text):
        # Only depth-1 blocks: count parens from file start to match position.
        depth = 0
        for ch in text[: m.start()]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if depth != 1:
            continue
        depth, end = 0, None
        for i in range(m.start(), len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError(f"unbalanced symbol block for {m.group(1)!r}")
        blocks[m.group(1)] = text[m.start() : end]
    return blocks


def _get(sx: Sexp, key: str):
    """First child list starting with `key`, or None."""
    for item in sx:
        if isinstance(item, list) and item and item[0] == key:
            return item
    return None


def _has_flag(sx: Sexp, key: str) -> bool:
    """True for bare `key`, `(key)`, or `(key <anything-but-no>)`.

    Covers all observed spellings: bare `hide` (legacy), `(hide yes)`,
    and `(power global)` / `(power local)` in KiCad 10 power symbols.
    """
    for item in sx:
        if item == key:
            return True
        if isinstance(item, list) and item and item[0] == key:
            return len(item) < 2 or item[1] not in ("no", False)
    return False


_UNIT_RE = re.compile(r"^(?P<base>.+)_(?P<unit>\d+)_(?P<body>\d+)$")


def _parse_pins(symbol_sx: list) -> list[PinDef]:
    """Collect pins from a parsed `(symbol "Name" ...)` including unit blocks."""
    pins: list[PinDef] = []
    for item in symbol_sx:
        if not (isinstance(item, list) and item and item[0] == "symbol"):
            continue
        unit_name = str(item[1])
        m = _UNIT_RE.match(unit_name)
        unit = int(m.group("unit")) if m else 0
        for sub in item:
            if not (isinstance(sub, list) and sub and sub[0] == "pin"):
                continue
            etype_token = str(sub[1])
            at = _get(Sexp(sub) if not isinstance(sub, Sexp) else sub, "at") or ["at", 0, 0, 0]
            length = _get(sub, "length") or ["length", 0]
            name = _get(sub, "name") or ["name", "~"]
            number = _get(sub, "number") or ["number", ""]
            pins.append(
                PinDef(
                    number=str(number[1]),
                    name=str(name[1]),
                    etype=KICAD_PIN_TYPES[etype_token],
                    x=float(at[1]),
                    y=float(at[2]),
                    orientation=int(at[3]) if len(at) > 3 else 0,
                    length=float(length[1]),
                    hidden=_has_flag(sub, "hide"),
                    unit=unit,
                )
            )
    return pins


def _property_value(symbol_sx: list, prop_name: str) -> str | None:
    for item in symbol_sx:
        if (
            isinstance(item, list)
            and item
            and item[0] == "property"
            and len(item) >= 3
            and str(item[1]) == prop_name
        ):
            return str(item[2])
    return None


def parse_library(path: str | Path, lib_nickname: str | None = None) -> dict[str, SymbolDef]:
    """Parse one .kicad_sym file → {lib_id: SymbolDef}, resolving extends."""
    path = Path(path)
    nickname = lib_nickname or path.stem
    text = path.read_text(encoding="utf-8")
    raw_blocks = _extract_toplevel_blocks(text)

    parsed: dict[str, list] = {}
    root = Sexp(text)
    for item in root:
        if isinstance(item, list) and item and item[0] == "symbol":
            parsed[str(item[1])] = item

    defs: dict[str, SymbolDef] = {}

    def build(name: str, seen: tuple[str, ...] = ()) -> SymbolDef:
        lib_id = f"{nickname}:{name}"
        if lib_id in defs:
            return defs[lib_id]
        if name in seen:
            raise ValueError(f"extends cycle at {name}")
        sx = parsed[name]
        extends = _get(sx, "extends")
        if extends is not None:
            parent = build(str(extends[1]), seen + (name,))
            pins = [PinDef(**vars(p)) for p in parent.pins]
            is_power = parent.is_power or _has_flag(sx, "power")
            ref = _property_value(sx, "Reference") or parent.reference_prefix
        else:
            pins = _parse_pins(sx)
            is_power = _has_flag(sx, "power")
            ref = _property_value(sx, "Reference") or "U"
        d = SymbolDef(
            lib_id=lib_id,
            raw_sexp=raw_blocks[name],
            pins=pins,
            is_power=is_power,
            reference_prefix=ref,
        )
        defs[lib_id] = d
        return d

    for name in parsed:
        build(name)
    return defs


def load_symbols(lib_ids: list[str], symbol_dir: Path = KICAD_SYMBOL_DIR) -> dict[str, SymbolDef]:
    """Load specific symbols ("Device:R", ...) from bundled libraries.

    Parses each needed library file once. Phase 2 replaces this with the
    SQLite-indexed multi-library search; the emitter interface stays the same.
    """
    wanted: dict[str, list[str]] = {}
    for lib_id in lib_ids:
        lib, _, name = lib_id.partition(":")
        wanted.setdefault(lib, []).append(name)

    out: dict[str, SymbolDef] = {}
    for lib, names in wanted.items():
        all_defs = parse_library(symbol_dir / f"{lib}.kicad_sym", lib)
        for name in names:
            lib_id = f"{lib}:{name}"
            if lib_id not in all_defs:
                raise KeyError(f"symbol {lib_id} not found in {lib}.kicad_sym")
            out[lib_id] = all_defs[lib_id]
    return out
