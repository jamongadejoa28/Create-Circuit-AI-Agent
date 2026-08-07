"""Curated knowledge base + search_knowledge API (plan §6).

The knowledge lives as reviewed JSON files in data/knowledge/*.json —
NOT as bulk-extracted PDF text. Per the plan's investigation (§6.2), naive
extraction destroys exactly the high-value content (tables, formulas), so
entries are curated per the 3-tier pipeline and each carries its source
citation (book, section, PDF page index, extraction tier). Entry texts
are short derived statements of engineering facts, not reproductions of
book prose (plan §13: no redistribution).

Every entry must pass the reachability test (§6.3): it is a value formula,
a component-selection rule, or a condition an §8.2 ERC rule can consume —
"interesting background" does not get in.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = _PROJECT / "data" / "knowledge"
DEFAULT_DB = _PROJECT / "data" / "knowledge.sqlite"

REQUIRED_FIELDS = {"id", "type", "statement", "tags", "source"}
VALID_TYPES = {"component_rule", "formula", "table", "example", "convention"}


def load_entries(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[dict]:
    """Load and validate all curated entries; raises on malformed data."""
    entries: list[dict] = []
    seen: set[str] = set()
    for f in sorted(knowledge_dir.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{f.name}: top level must be a list of entries")
        for e in data:
            missing = REQUIRED_FIELDS - set(e)
            if missing:
                raise ValueError(f"{f.name}: entry {e.get('id', '?')} missing {sorted(missing)}")
            if e["type"] not in VALID_TYPES:
                raise ValueError(f"{f.name}: entry {e['id']} has invalid type {e['type']!r}")
            if e["id"] in seen:
                raise ValueError(f"duplicate knowledge id {e['id']!r}")
            if "book" not in e["source"]:
                raise ValueError(f"{f.name}: entry {e['id']} source lacks 'book'")
            seen.add(e["id"])
            e["_file"] = f.name
            entries.append(e)
    return entries


def build_index(
    db_path: str | Path = DEFAULT_DB, knowledge_dir: Path = KNOWLEDGE_DIR
) -> int:
    """(Re)build the FTS index from the JSON files; returns entry count."""
    entries = load_entries(knowledge_dir)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE entries (id TEXT PRIMARY KEY, json TEXT NOT NULL);
        CREATE VIRTUAL TABLE entries_fts USING fts5(
            id UNINDEXED, statement, condition, tags, type
        );
        """
    )
    for e in entries:
        con.execute("INSERT INTO entries VALUES (?,?)", (e["id"], json.dumps(e, ensure_ascii=False)))
        con.execute(
            "INSERT INTO entries_fts VALUES (?,?,?,?,?)",
            (e["id"], e["statement"], e.get("condition", ""), " ".join(e["tags"]), e["type"]),
        )
    con.commit()
    con.close()
    return len(entries)


def _fts_query(query: str) -> str:
    tokens = [t.replace('"', '""') for t in query.split() if t]
    # OR semantics: grounding queries are descriptive phrases, not all of
    # whose words appear in any single entry.
    return " OR ".join(f'"{t}"' for t in tokens)


class KnowledgeIndex:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"knowledge index not built: {self.db_path} — run scripts/build_knowledge_index.py"
            )
        self.con = sqlite3.connect(self.db_path)

    def search_knowledge(self, query: str, limit: int = 3) -> list[dict]:
        """Trimmed grounding payload for the LLM (context budget, §7.3)."""
        q = _fts_query(query)
        if not q:
            return []
        rows = self.con.execute(
            """
            SELECT e.json FROM entries_fts f JOIN entries e ON e.id = f.id
            WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts) LIMIT ?
            """,
            (q, limit),
        ).fetchall()
        out = []
        for (raw,) in rows:
            e = json.loads(raw)
            item = {
                "id": e["id"],
                "type": e["type"],
                "statement": e["statement"],
                "source": f'{e["source"]["book"]} — {e["source"].get("section", "")}',
            }
            if "formula" in e:
                item["formula"] = e["formula"]
            if "values" in e:
                item["values"] = e["values"]
            if "erc_rule" in e:
                item["erc_rule"] = e["erc_rule"]
            out.append(item)
        return out
