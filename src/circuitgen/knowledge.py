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
import re
import sqlite3
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = _PROJECT / "data" / "knowledge"
DEFAULT_DB = _PROJECT / "data" / "knowledge.sqlite"

REQUIRED_FIELDS = {"id", "type", "statement", "tags", "source"}
# Original curated types plus the Codex knowledge-tier taxonomy
# (2026-08-08): tier 1 = executable rules, 2 = circuit patterns,
# 3 = selection guidance / failure modes, 4 = worked designs,
# 5 = source evidence chunks. Entries may carry an integer "tier" field.
VALID_TYPES = {
    "component_rule", "formula", "table", "example", "convention",
    "device_rule", "circuit_pattern", "selection_guidance",
    "failure_mode", "worked_design", "source_evidence",
}

_QUERY_STOPWORDS = {
    "a", "an", "and", "or", "the", "for", "to", "of", "with", "on", "in", "from",
    # These are either grammatical or dangerously ambiguous in electronics
    # queries ("CAN" otherwise matches the ordinary English verb in prose).
    "type", "can", "fd",
}


def _search_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in _QUERY_STOPWORDS
    }


def _hit_tokens(entry: dict) -> set[str]:
    return _search_tokens(
        " ".join(
            [
                entry.get("statement", ""), entry.get("condition", ""),
                " ".join(entry.get("tags", [])), entry.get("type", ""),
            ]
        )
    )


def load_entries(
    knowledge_dir: Path = KNOWLEDGE_DIR, *, allow_internal_fixtures: bool = False
) -> list[dict]:
    """Load and validate curated production knowledge.

    ERC-passing examples are useful regression fixtures, but they are not an
    independent source of circuit-design truth.  Production indexing rejects
    them so a fixture cannot become LLM grounding through a misplaced JSON
    file.  Tests that inspect archived fixtures must opt in explicitly.
    """
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
            provenance = e["source"].get("provenance")
            if provenance == "internal-fixture" and not allow_internal_fixtures:
                raise ValueError(
                    f"{f.name}: entry {e['id']} is an internal fixture; "
                    "it cannot be indexed as production knowledge"
                )
            if provenance != "textbook" and not (
                allow_internal_fixtures and provenance == "internal-fixture"
            ):
                raise ValueError(
                    f"{f.name}: entry {e['id']} has unsupported provenance "
                    f"{provenance!r}"
                )
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

    def search_knowledge(
        self, query: str, limit: int = 3, *, include_score: bool = False,
        relevance_gate: bool = True,
    ) -> list[dict]:
        """Trimmed grounding payload for the LLM (context budget, §7.3)."""
        q = _fts_query(query)
        if not q:
            return []
        # Fetch beyond top-k because the lexical relevance gate may reject
        # one-token coincidences before the final limit is applied.
        fetch_limit = max(limit * 4, limit)
        rows = self.con.execute(
            """
            SELECT e.json, bm25(entries_fts) AS score
            FROM entries_fts f JOIN entries e ON e.id = f.id
            WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts) LIMIT ?
            """,
            (q, fetch_limit),
        ).fetchall()
        out = []
        query_tokens = _search_tokens(query)
        min_matches = 1 if len(query_tokens) <= 1 else 2
        for raw_rank, (raw, score) in enumerate(rows, start=1):
            e = json.loads(raw)
            matched = sorted(query_tokens & _hit_tokens(e))
            if relevance_gate and len(matched) < min_matches:
                continue
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
            if include_score:
                item["_retrieval"] = {
                    "rank": len(out) + 1, "raw_rank": raw_rank,
                    "bm25": score, "matched_tokens": matched,
                    "query_token_count": len(query_tokens),
                }
            out.append(item)
            if len(out) >= limit:
                break
        return out
