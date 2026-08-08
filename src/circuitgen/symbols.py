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
from functools import lru_cache

from .ir import PinDef, SymbolDef
from .pins import KICAD_PIN_TYPES

# Preferred source: the native kicad-symbols clone pinned to the 10.0.5 tag
# (per-symbol *.kicad_symdir layout — the single-file libraries in the
# Windows install are assembled from these at packaging time). Native ext4
# reads are ~6x faster than /mnt/c's 9P filesystem, and provenance becomes
# a git tag instead of an installer artifact. Falls back to the install.
_PROJECT = Path(__file__).resolve().parents[2]
_NATIVE_CLONE = _PROJECT / "kicad-symbols"
_WINDOWS_INSTALL = Path("/mnt/c/Program Files/KiCad/10.0/share/kicad/symbols")
KICAD_SYMBOL_DIR = (
    _NATIVE_CLONE if (_NATIVE_CLONE / "Device.kicad_symdir").is_dir() else _WINDOWS_INSTALL
)


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
    """Collect pins from a parsed `(symbol "Name" ...)` including unit blocks.

    Sub-blocks are named NAME_<unit>_<bodystyle>; body style 2 is the
    De Morgan alternate drawing of the SAME pins — collecting it would
    duplicate every pin (74LS00 would show each gate pin twice), so only
    body styles 0/1 contribute.
    """
    pins: list[PinDef] = []
    for item in symbol_sx:
        if not (isinstance(item, list) and item and item[0] == "symbol"):
            continue
        unit_name = str(item[1])
        m = _UNIT_RE.match(unit_name)
        unit = int(m.group("unit")) if m else 0
        if m and int(m.group("body")) > 1:
            continue
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


def _all_properties(symbol_sx: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in symbol_sx:
        if (
            isinstance(item, list)
            and item
            and item[0] == "property"
            and len(item) >= 3
        ):
            out[str(item[1])] = str(item[2])
    return out


def library_path(symbol_dir: Path, lib: str) -> Path | None:
    """Path of a library under symbol_dir in either layout, or None."""
    d = symbol_dir / f"{lib}.kicad_symdir"
    if d.is_dir():
        return d
    f = symbol_dir / f"{lib}.kicad_sym"
    return f if f.exists() else None


@lru_cache(maxsize=128)
def _parse_library_cached(path_str: str, nickname: str) -> dict[str, SymbolDef]:
    return _parse_library(Path(path_str), nickname)


def parse_library(path: str | Path, lib_nickname: str | None = None) -> dict[str, SymbolDef]:
    """Parse one library → {lib_id: SymbolDef}, memoized per (path, nickname).

    Parsing a library costs ~0.75-1.1 s (MCU_ST_STM32G4 is 1.07 s) and
    Agent._resolve_symbols asked for the same lib_ids 14 times in a single
    run, so the uncached version dominated both the agent (83 s of a 124 s
    run) and the test suite. SymbolDefs are never mutated anywhere in the
    tree, so sharing them is safe; callers copy the entries they want into
    their own dict.
    """
    path = Path(path)
    return _parse_library_cached(str(path), lib_nickname or path.stem)


def _parse_library(path: Path, lib_nickname: str | None = None) -> dict[str, SymbolDef]:
    """Parse one library → {lib_id: SymbolDef}, resolving extends.

    Accepts either a single .kicad_sym file (Windows-install / vendor
    layout) or a *.kicad_symdir directory (official kicad-symbols repo:
    one file per symbol; extends parents are sibling files, so the whole
    directory is one namespace).
    """
    path = Path(path)
    nickname = lib_nickname or path.stem
    if path.is_dir():
        texts = [f.read_text(encoding="utf-8") for f in sorted(path.glob("*.kicad_sym"))]
    else:
        texts = [path.read_text(encoding="utf-8")]

    raw_blocks: dict[str, str] = {}
    parsed: dict[str, list] = {}
    for text in texts:
        raw_blocks.update(_extract_toplevel_blocks(text))
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
            props = dict(parent.properties) | _all_properties(sx)
            # parent.raw_sexp is itself already flattened, so chains compose.
            raw = _flatten_extends(parent.raw_sexp, parent_name, raw_blocks[name], name)
        else:
            pins = _parse_pins(sx)
            is_power = _has_flag(sx, "power")
            ref = _property_value(sx, "Reference") or "U"
            props = _all_properties(sx)
            raw = raw_blocks[name]
        d = SymbolDef(
            lib_id=lib_id,
            raw_sexp=raw,
            pins=pins,
            is_power=is_power,
            reference_prefix=ref,
            properties=props,
        )
        defs[lib_id] = d
        return d

    for name in parsed:
        build(name)
    return defs


def load_symbols(
    lib_ids: list[str],
    symbol_dir: Path = KICAD_SYMBOL_DIR,
    strict: bool = True,
) -> dict[str, SymbolDef]:
    """Load specific symbols ("Device:R", ...) from bundled libraries.

    With strict=False, unknown libraries/symbols are silently omitted from
    the result instead of raising — the pipeline uses this so that an
    LLM-invented lib_id surfaces as a structured unknown_symbol self-ERC
    error (repairable) rather than as a crash.
    """
    wanted: dict[str, list[str]] = {}
    for lib_id in lib_ids:
        lib, _, name = lib_id.partition(":")
        wanted.setdefault(lib, []).append(name)

    out: dict[str, SymbolDef] = {}
    for lib, names in wanted.items():
        lib_path = library_path(symbol_dir, lib)
        if lib_path is None:
            if strict:
                raise KeyError(f"library {lib} not found in {symbol_dir}")
            continue
        all_defs = parse_library(lib_path, lib)
        for name in names:
            lib_id = f"{lib}:{name}"
            if lib_id not in all_defs:
                if strict:
                    raise KeyError(f"symbol {lib_id} not found in {lib}.kicad_sym")
                continue
            out[lib_id] = all_defs[lib_id]
    return out
