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


_SYMBOL_NAME_RE = re.compile(r'\(symbol\s+"((?:[^"\\]|\\.)*)"')


def _extract_toplevel_blocks(text: str) -> dict[str, str]:
    """Cut verbatim depth-1 `(symbol "NAME" ...)` blocks out of library text.

    Single linear scan (the naive per-match depth recount is O(n²) and takes
    minutes on multi-MB libraries), and quote-aware: parentheses inside
    quoted strings — common in Description properties like "(dual)" — must
    not count toward nesting depth.
    """
    blocks: dict[str, str] = {}
    depth = 0
    in_str = False
    block_start: int | None = None
    block_name: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
            if depth == 2 and block_start is None and text.startswith("symbol", i + 1):
                m = _SYMBOL_NAME_RE.match(text, i)
                if m:
                    block_start, block_name = i, m.group(1)
        elif ch == ")":
            depth -= 1
            if depth == 1 and block_start is not None:
                blocks[block_name] = text[block_start : i + 1]
                block_start = block_name = None
        i += 1
    if in_str or depth != 0 or block_start is not None:
        raise ValueError("unbalanced or unterminated symbol library text")
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


def _cut_balanced(text: str, start: int) -> str:
    """The complete parenthesized block starting at text[start] == '('.

    Quote-aware: parens inside quoted strings don't count.
    """
    depth = 0
    in_str = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise ValueError("unbalanced block")


def _property_blocks(block_text: str) -> dict[str, str]:
    """{property name: verbatim (property ...) block} for one symbol block."""
    out: dict[str, str] = {}
    for m in re.finditer(r'\(property\s+"((?:[^"\\]|\\.)*)"', block_text):
        out[m.group(1)] = _cut_balanced(block_text, m.start())
    return out


def _flatten_extends(parent_raw: str, parent_name: str, derived_raw: str, derived_name: str) -> str:
    """Replicate LIB_SYMBOL::Flatten() textually: parent's full body under
    the derived name, with the derived block's property overrides applied.

    KiCad never writes `extends` into a schematic's lib_symbols cache
    (sch_screen.cpp always calls Flatten() before caching); an embedded
    derived block is silently pin-less. Derived library blocks contain only
    property overrides, which is exactly what Flatten copies over.
    """
    text = parent_raw
    # Outer name, then inner unit blocks "<parent>_<unit>_<body>".
    text = re.sub(
        r'\(symbol\s+"' + re.escape(parent_name) + '"',
        lambda m: f'(symbol "{derived_name}"',
        text,
        count=1,
    )
    text = re.sub(
        r'\(symbol\s+"' + re.escape(parent_name) + r'_(\d+_\d+)"',
        lambda m: f'(symbol "{derived_name}_{m.group(1)}"',
        text,
    )
    overrides = _property_blocks(derived_raw)
    for name, new_block in overrides.items():
        existing = None
        for m in re.finditer(r'\(property\s+"((?:[^"\\]|\\.)*)"', text):
            if m.group(1) == name:
                existing = (m.start(), m.start() + len(_cut_balanced(text, m.start())))
                break
        if existing:
            text = text[: existing[0]] + new_block + text[existing[1] :]
        else:
            first_unit = text.find('(symbol "')
            first_unit = text.find('(symbol "', first_unit + 1)  # skip outer
            if first_unit == -1:
                first_unit = text.rfind(")")
            text = text[:first_unit] + new_block + "\n\t\t" + text[first_unit:]
    return text


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
            parent_name = str(extends[1])
            parent = build(parent_name, seen + (name,))
            pins = [PinDef(**vars(p)) for p in parent.pins]
            is_power = parent.is_power or _has_flag(sx, "power")
            ref = _property_value(sx, "Reference") or parent.reference_prefix
            # parent.raw_sexp is itself already flattened, so chains compose.
            raw = _flatten_extends(parent.raw_sexp, parent_name, raw_blocks[name], name)
        else:
            pins = _parse_pins(sx)
            is_power = _has_flag(sx, "power")
            ref = _property_value(sx, "Reference") or "U"
            raw = raw_blocks[name]
        d = SymbolDef(
            lib_id=lib_id,
            raw_sexp=raw,
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
