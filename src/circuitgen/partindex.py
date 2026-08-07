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
"""


@dataclass
class LibrarySource:
    root: Path
    nickname_prefix: str  # "" for bundled
    priority: int
    license: str


def default_sources() -> list[LibrarySource]:
    project = Path(__file__).resolve().parents[2]
    return [
        LibrarySource(KICAD_SYMBOL_DIR, "", 1, "CC-BY-SA-4.0 WITH KiCad-libraries-exception"),
        LibrarySource(project / "ESP-kicad-libraries" / "symbols", "ESP_", 2, "CC-BY-SA-4.0 WITH exception"),
        LibrarySource(project / "SparkFun-KiCad-Libraries" / "symbols", "SparkFun_", 2, "CC-BY-4.0"),
        LibrarySource(project / "OLIMEX-kicad" / "KiCAD_Components" / "Used-In-KiCad_v7", "OLIMEX_", 3, "Apache-2.0"),
    ]


def build_index(
    db_path: str | Path = DEFAULT_DB,
    sources: list[LibrarySource] | None = None,
    on_progress=None,
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
        for f in sorted(src.root.rglob("*.kicad_sym")):
            nickname = src.nickname_prefix + f.stem
            try:
                defs = parse_library(f, nickname)
            except Exception as e:  # a broken vendor file must not kill the build
                stats["errors"].append(f"{f}: {e!r}")
                continue
            checksum = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
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

    def provenance(self, lib_id: str) -> dict:
        row = self.con.execute(
            "SELECT l.* FROM symbols s JOIN libraries l USING (nickname) WHERE s.lib_id = ?",
            (lib_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown lib_id {lib_id!r}")
        return dict(row)
