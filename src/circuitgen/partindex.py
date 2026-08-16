"""Multi-library part index: SQLite + FTS5 (plan §5.3).

Priorities (measured formats, corrected from the Codex draft):
  1. KiCad bundled libraries (reference set)
  2. ESP / SparkFun (modern .kicad_sym)
  3. OLIMEX Used-In-KiCad_v7
DigiKey stays out until its legacy .lib/.dcm are converted with
`kicad-cli sym upgrade` (100% legacy — cannot be indexed as-is).

Vendor libraries are namespaced by prefixing the file stem (ESP_*,
SparkFun_*, OLIMEX_*) so same-named files can never shadow bundled ones;
our schematics embed symbols, so nicknames only need to be consistent
inside this system.

The search/pin APIs return deliberately trimmed payloads — they are the
LLM tool surface (search_parts / get_part_pins) and must respect the 8k
context budget (plan §7.3): no geometry, no raw s-expressions, few rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .symbols import KICAD_SYMBOL_DIR, parse_library

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "parts.sqlite"

_SCHEMA = """
CREATE TABLE libraries (
    nickname TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    priority INTEGER NOT NULL,
    license TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE TABLE symbols (
    lib_id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL REFERENCES libraries(nickname),
    name TEXT NOT NULL,
    reference_prefix TEXT NOT NULL,
    is_power INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    fp_filters TEXT NOT NULL DEFAULT '',
    footprint TEXT NOT NULL DEFAULT '',
    datasheet TEXT NOT NULL DEFAULT '',
    unit_count INTEGER NOT NULL,
    pin_count INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    unit0_mix INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pins (
    lib_id TEXT NOT NULL REFERENCES symbols(lib_id),
    number TEXT NOT NULL,
    name TEXT NOT NULL,
    etype TEXT NOT NULL,
    unit INTEGER NOT NULL,
    hidden INTEGER NOT NULL
);
CREATE INDEX pins_by_symbol ON pins(lib_id);
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    lib_id UNINDEXED, name, description, keywords
);
CREATE TABLE footprints (
    fp_id TEXT PRIMARY KEY,      -- "Resistor_SMD:R_0805_2012Metric"
    lib TEXT NOT NULL,
    name TEXT NOT NULL,
    descr TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    pad_count INTEGER NOT NULL,  -- distinct electrical pad numbers
    fp_type TEXT NOT NULL DEFAULT ''  -- smd | through_hole | ''
);
CREATE TABLE fp_pads (
    fp_id TEXT NOT NULL REFERENCES footprints(fp_id),
    number TEXT NOT NULL
);
CREATE INDEX fp_pads_by_fp ON fp_pads(fp_id);
"""

_PAD_RE = None  # compiled lazily in _parse_footprint_meta


def _parse_footprint_meta(path: Path) -> dict:
    """name/descr/tags/pads from one .kicad_mod — regex-level, no full parse.

    Pad numbers repeat for thermal/stacked pads; the distinct non-empty set
    is what a symbol's pin numbers must map onto.
    """
    global _PAD_RE
    import re as _re

    if _PAD_RE is None:
        _PAD_RE = {
            "pad": _re.compile(r'\(pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))'),
            "descr": _re.compile(r'\(descr\s+"((?:[^"\\]|\\.)*)"'),
            "tags": _re.compile(r'\(tags\s+"((?:[^"\\]|\\.)*)"'),
            "attr": _re.compile(r"\(attr\s+([a-z_]+)"),
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    pads = set()
    for m in _PAD_RE["pad"].finditer(text):
        num = m.group(1) if m.group(1) is not None else m.group(2)
        if num:
            pads.add(num)
    d = _PAD_RE["descr"].search(text)
    t = _PAD_RE["tags"].search(text)
    a = _PAD_RE["attr"].search(text)
    return {
        "name": path.stem,
        "descr": d.group(1) if d else "",
        "tags": t.group(1) if t else "",
        "pads": pads,
        "fp_type": a.group(1) if a else "",
    }


@dataclass
class LibrarySource:
    root: Path
    nickname_prefix: str  # "" for bundled
    priority: int
    license: str


def default_sources() -> list[LibrarySource]:
    project = Path(__file__).resolve().parents[2]
    return [
        # native kicad-symbols clone pinned to tag 10.0.5 (falls back to the
        # Windows install via the KICAD_SYMBOL_DIR resolver)
        LibrarySource(KICAD_SYMBOL_DIR, "", 1, "CC-BY-SA-4.0 WITH KiCad-libraries-exception (kicad-symbols tag 10.0.5)"),
        LibrarySource(project / "ESP-kicad-libraries" / "symbols", "ESP_", 2, "CC-BY-SA-4.0 WITH exception"),
        LibrarySource(project / "SparkFun-KiCad-Libraries" / "symbols", "SparkFun_", 2, "CC-BY-4.0"),
        LibrarySource(project / "OLIMEX-kicad" / "KiCAD_Components" / "Used-In-KiCad_v7", "OLIMEX_", 3, "Apache-2.0"),
    ]


def _library_paths(root: Path) -> list[Path]:
    """Libraries under a source root: *.kicad_symdir directories (official
    repo layout) plus standalone .kicad_sym files not inside a symdir."""
    symdirs = sorted(root.glob("*.kicad_symdir"))
    files = sorted(
        p for p in root.rglob("*.kicad_sym") if p.parent.suffix != ".kicad_symdir"
    )
    return symdirs + files


def _library_checksum(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_dir():
        for f in sorted(path.glob("*.kicad_sym")):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    else:
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def default_footprint_root() -> Path | None:
    root = Path(__file__).resolve().parents[2] / "kicad-footprints"
    return root if root.is_dir() else None


def build_index(
    db_path: str | Path = DEFAULT_DB,
    sources: list[LibrarySource] | None = None,
    on_progress=None,
    footprint_root: Path | None = None,
) -> dict:
    """(Re)build the index from scratch. Returns summary counts."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)

    stats = {"libraries": 0, "symbols": 0, "pins": 0, "errors": []}
    for src in sources or default_sources():
        if not src.root.exists():
            stats["errors"].append(f"missing source root: {src.root}")
            continue
        for f in _library_paths(src.root):
            nickname = src.nickname_prefix + f.stem
            try:
                defs = parse_library(f, nickname)
            except Exception as e:  # a broken vendor file must not kill the build
                stats["errors"].append(f"{f}: {e!r}")
                continue
            checksum = _library_checksum(f)
            con.execute(
                "INSERT INTO libraries VALUES (?,?,?,?,?)",
                (nickname, str(f), src.priority, src.license, checksum),
            )
            stats["libraries"] += 1
            for lib_id, d in defs.items():
                props = d.properties
                units = {p.unit for p in d.pins}
                unit0_mix = int(0 in units and bool(units - {0}))
                con.execute(
                    "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        lib_id,
                        nickname,
                        lib_id.split(":", 1)[1],
                        d.reference_prefix,
                        int(d.is_power),
                        props.get("Description", ""),
                        props.get("ki_keywords", ""),
                        props.get("ki_fp_filters", ""),
                        props.get("Footprint", ""),
                        props.get("Datasheet", ""),
                        len(d.placed_units()),
                        len(d.pins),
                        src.priority,
                        unit0_mix,
                    ),
                )
                con.executemany(
                    "INSERT INTO pins VALUES (?,?,?,?,?,?)",
                    [
                        (lib_id, p.number, p.name, p.etype.name, p.unit, int(p.hidden))
                        for p in d.pins
                    ],
                )
                con.execute(
                    "INSERT INTO symbols_fts VALUES (?,?,?,?)",
                    (lib_id, lib_id.split(":", 1)[1], props.get("Description", ""), props.get("ki_keywords", "")),
                )
                stats["symbols"] += 1
                stats["pins"] += len(d.pins)
            if on_progress:
                on_progress(nickname, len(defs))

    # --- footprints (official kicad-footprints clone): existence, pad
    # numbers for pin↔pad matching, descr/tags for future search ---
    stats["footprints"] = 0
    fp_root = footprint_root or default_footprint_root()
    if fp_root is not None:
        for pretty in sorted(fp_root.glob("*.pretty")):
            lib = pretty.stem
            for mod in sorted(pretty.glob("*.kicad_mod")):
                try:
                    meta = _parse_footprint_meta(mod)
                except Exception as e:
                    stats["errors"].append(f"{mod}: {e!r}")
                    continue
                fp_id = f"{lib}:{meta['name']}"
                con.execute(
                    "INSERT OR REPLACE INTO footprints VALUES (?,?,?,?,?,?,?)",
                    (fp_id, lib, meta["name"], meta["descr"], meta["tags"], len(meta["pads"]), meta["fp_type"]),
                )
                con.executemany(
                    "INSERT INTO fp_pads VALUES (?,?)",
                    [(fp_id, n) for n in sorted(meta["pads"])],
                )
                stats["footprints"] += 1
            if on_progress:
                on_progress(f"fp:{lib}", stats["footprints"])

    con.commit()
    con.close()
    return stats


def _fts_query(query: str) -> str:
    """Quote each token so FTS5 syntax characters ('+5V', 'R_0805') are literal."""
    tokens = [t.replace('"', '""') for t in query.split() if t]
    return " ".join(f'"{t}"' for t in tokens)


class PartIndex:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"part index not built: {self.db_path} — run scripts/build_part_index.py"
            )
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row

    def search_parts(self, query: str, limit: int = 5) -> list[dict]:
        """Trimmed search results, best first.

        Ranking: exact symbol-name match beats everything (a query "LED"
        must surface Device:LED before every part whose description merely
        mentions LEDs), then bm25, then source priority, then simplicity
        (fewer pins first).
        """
        q = _fts_query(query)
        if not q:
            return []
        rows = self.con.execute(
            """
            SELECT s.lib_id, s.description, s.keywords, s.reference_prefix,
                   s.is_power, s.unit_count, s.pin_count, s.fp_filters,
                   s.footprint, s.priority, bm25(symbols_fts) AS rank
            FROM symbols_fts JOIN symbols s ON s.lib_id = symbols_fts.lib_id
            WHERE symbols_fts MATCH ? AND s.unit0_mix = 0
            ORDER BY (lower(s.name) = lower(?)) DESC, rank, s.priority, s.pin_count
            LIMIT ?
            """,
            (q, query.strip(), limit),
        ).fetchall()
        if not rows:
            rows = self._prefix_fallback(query, limit)
        return [
            {
                "lib_id": r["lib_id"],
                "description": r["description"][:160],
                "keywords": r["keywords"][:80],
                "reference_prefix": r["reference_prefix"],
                "is_power": bool(r["is_power"]),
                "units": r["unit_count"],
                "pins": r["pin_count"],
                "footprint_filters": r["fp_filters"][:80],
                "default_footprint": r["footprint"],
            }
            for r in rows
        ]

    def exact_symbol_ids(self, name: str) -> list[str]:
        """Catalog IDs whose symbol name exactly equals ``name``.

        FTS tokenization treats punctuation in ordering codes as operators,
        so exact names such as ``NE555D`` and ``ATmega328P-AU`` can be absent
        from otherwise sensible searches. Transcription already has the part
        identity; it should not replace it with a fuzzy neighbor.
        """
        text = (name or "").strip()
        if not text:
            return []
        rows = self.con.execute(
            """
            SELECT lib_id FROM symbols
            WHERE lower(name) = lower(?) AND unit0_mix = 0
            ORDER BY priority, pin_count, lib_id
            """,
            (text,),
        ).fetchall()
        return [str(row["lib_id"]) for row in rows]

    def exact_lib_id(self, lib_id: str) -> str | None:
        """Return a catalog lib_id only when the full ``Library:Symbol`` exists."""
        text = (lib_id or "").strip()
        if ":" not in text:
            return None
        # A verified full ID may name a single-unit symbol whose common power
        # pins live in unit 0 (Timer:NE555D). The emitter supports that case;
        # fuzzy search still excludes unit0_mix so it cannot accidentally pick
        # a structurally unusual symbol.
        row = self.con.execute(
            "SELECT lib_id FROM symbols WHERE lower(lib_id) = lower(?)",
            (text,),
        ).fetchone()
        return str(row["lib_id"]) if row else None

    def _prefix_fallback(self, query: str, limit: int) -> list:
        """Part-number prefix search when FTS misses.

        Ordering codes rarely match symbol names exactly (STM32G474RET6 vs
        symbol STM32G474RETx), so retry the longest token as a shrinking
        name prefix until something matches.
        """
        tokens = sorted((t for t in query.split() if len(t) >= 6), key=len, reverse=True)
        for tok in tokens[:2]:
            for cut in range(len(tok), max(5, len(tok) - 5), -1):
                rows = self.con.execute(
                    """
                    SELECT s.lib_id, s.description, s.keywords, s.reference_prefix,
                           s.is_power, s.unit_count, s.pin_count, s.fp_filters,
                           s.footprint, s.priority, 0 AS rank
                    FROM symbols s
                    WHERE s.name LIKE ? AND s.unit0_mix = 0
                    ORDER BY s.priority, length(s.name), s.name
                    LIMIT ?
                    """,
                    (tok[:cut] + "%", limit),
                ).fetchall()
                if rows:
                    return rows
        return []

    def get_part_pins(self, lib_id: str) -> list[dict]:
        """Full pin table of one symbol — numbers/names/types/units only."""
        rows = self.con.execute(
            "SELECT number, name, etype, unit, hidden FROM pins WHERE lib_id = ? ORDER BY unit, number",
            (lib_id,),
        ).fetchall()
        if not rows:
            raise KeyError(f"unknown lib_id {lib_id!r}")
        return [
            {
                "number": r["number"],
                "name": r["name"],
                "type": r["etype"],
                "unit": r["unit"],
                "hidden": bool(r["hidden"]),
            }
            for r in rows
        ]

    def symbol_source(self, lib_id: str) -> tuple[Path, str]:
        """(library file path, nickname) for loading the full SymbolDef."""
        row = self.con.execute(
            "SELECT l.source_file, l.nickname FROM symbols s JOIN libraries l USING (nickname) WHERE s.lib_id = ?",
            (lib_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown lib_id {lib_id!r}")
        return Path(row["source_file"]), row["nickname"]

    def load_symbols(self, lib_ids: list[str]) -> dict:
        """Full SymbolDefs for emission, resolved through the index."""
        by_file: dict[tuple[Path, str], list[str]] = {}
        for lib_id in lib_ids:
            by_file.setdefault(self.symbol_source(lib_id), []).append(lib_id)
        out = {}
        for (path, nickname), ids in by_file.items():
            defs = parse_library(path, nickname)
            for lib_id in ids:
                out[lib_id] = defs[lib_id]
        return out

    # ---- footprints ----

    def has_footprints(self) -> bool:
        row = self.con.execute("SELECT COUNT(*) c FROM footprints").fetchone()
        return row["c"] > 0

    def footprint_pads(self, fp_id: str) -> set[str] | None:
        """Distinct pad numbers of a footprint, or None if unknown."""
        if self.con.execute(
            "SELECT 1 FROM footprints WHERE fp_id = ?", (fp_id,)
        ).fetchone() is None:
            return None
        rows = self.con.execute(
            "SELECT number FROM fp_pads WHERE fp_id = ?", (fp_id,)
        ).fetchall()
        return {r["number"] for r in rows}

    def _all_footprint_names(self) -> list[tuple[str, str, int]]:
        if not hasattr(self, "_fp_cache"):
            self._fp_cache = [
                (r["fp_id"], r["name"], r["pad_count"])
                for r in self.con.execute("SELECT fp_id, name, pad_count FROM footprints")
            ]
        return self._fp_cache

    def match_footprints(
        self, filters: list[str], required_pins: set[str], limit: int = 10
    ) -> list[str]:
        """Footprints matching KiCad fp_filters whose pads cover required_pins.

        Filter semantics per KiCad: '*'/'?' globs, case-insensitive; a
        pattern containing ':' matches the full "Lib:Name", otherwise the
        bare name. Results ordered: exact pad-count match first, then
        preferred common sizes (0805 > 0603 — the golden circuits' default
        scale), then name.
        """
        import fnmatch

        hits = []
        for fp_id, name, pad_count in self._all_footprint_names():
            for pat in filters:
                target = fp_id if ":" in pat else name
                if fnmatch.fnmatch(target.lower(), pat.lower()):
                    hits.append((fp_id, pad_count))
                    break
        good = []
        for fp_id, pad_count in hits:
            pads = self.footprint_pads(fp_id)
            if pads is not None and required_pins <= pads:
                good.append((fp_id, pad_count))

        def rank(item):
            fp_id, pad_count = item
            return (
                pad_count != len(required_pins),
                "0805" not in fp_id,
                "0603" not in fp_id,
                fp_id,
            )

        return [fp_id for fp_id, _ in sorted(good, key=rank)[:limit]]

    def provenance(self, lib_id: str) -> dict:
        row = self.con.execute(
            "SELECT l.* FROM symbols s JOIN libraries l USING (nickname) WHERE s.lib_id = ?",
            (lib_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown lib_id {lib_id!r}")
        return dict(row)
