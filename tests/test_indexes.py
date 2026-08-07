"""Part index + knowledge index (Phase 2 tool surface for the agent)."""

from pathlib import Path

import pytest

from circuitgen.knowledge import KNOWLEDGE_DIR, KnowledgeIndex, build_index as build_kn, load_entries
from circuitgen.partindex import LibrarySource, PartIndex, build_index as build_parts
from circuitgen.symbols import KICAD_SYMBOL_DIR

pytestmark = pytest.mark.skipif(
    not KICAD_SYMBOL_DIR.exists(), reason="KiCad bundled libraries not mounted"
)


@pytest.fixture(scope="module")
def small_part_index(tmp_path_factory):
    """Index over a 3-library subset — fast enough for every test run."""
    import shutil

    from circuitgen.symbols import library_path

    tmp = tmp_path_factory.mktemp("pidx")
    subset = tmp / "libs"
    subset.mkdir()
    for name in ("Device", "Switch", "power", "74xx"):
        src = library_path(KICAD_SYMBOL_DIR, name)
        if src.is_dir():
            shutil.copytree(src, subset / src.name)
        else:
            shutil.copy(src, subset / src.name)
    db = tmp / "parts.sqlite"
    stats = build_parts(db, sources=[LibrarySource(subset, "", 1, "CC-BY-SA-4.0")])
    assert stats["errors"] == []
    assert stats["symbols"] > 100
    return PartIndex(db)


def test_search_finds_canonical_parts(small_part_index):
    idx = small_part_index
    assert idx.search_parts("resistor")[0]["lib_id"] == "Device:R"
    assert idx.search_parts("push button switch")[0]["lib_id"] == "Switch:SW_Push"
    hits = {h["lib_id"] for h in idx.search_parts("quad NAND 2-input", limit=8)}
    assert any(h.startswith("74xx:74") for h in hits)


def test_search_results_are_trimmed(small_part_index):
    hit = small_part_index.search_parts("resistor", limit=1)[0]
    assert "raw" not in hit and "sexp" not in str(hit).lower()
    assert len(str(hit)) < 500  # context-budget guard


def test_pin_lookup_and_unknown(small_part_index):
    pins = small_part_index.get_part_pins("74xx:74LS00")
    assert len(pins) == 14
    assert {p["unit"] for p in pins} == {1, 2, 3, 4, 5}
    with pytest.raises(KeyError):
        small_part_index.get_part_pins("Nope:Nothing")


def test_load_symbols_through_index(small_part_index):
    syms = small_part_index.load_symbols(["Device:LED", "power:GND"])
    assert syms["Device:LED"].pins[0].name == "K"
    assert syms["power:GND"].is_power


def test_provenance_carries_license(small_part_index):
    p = small_part_index.provenance("Device:R")
    assert p["license"].startswith("CC-BY-SA")
    assert p["checksum"]


def test_knowledge_entries_valid():
    entries = load_entries()
    assert len(entries) >= 8
    ids = {e["id"] for e in entries}
    assert {"decoupling-cap-per-ic", "pullup-resistor-sizing", "led-series-resistor"} <= ids


def test_knowledge_search(tmp_path):
    db = tmp_path / "kn.sqlite"
    n = build_kn(db, KNOWLEDGE_DIR)
    assert n >= 8
    idx = KnowledgeIndex(db)
    top = idx.search_knowledge("decoupling capacitor value for IC")
    assert top and top[0]["id"].startswith("decoupling")
    top = idx.search_knowledge("pull-up resistor size")
    assert any("pullup" in h["id"] for h in top)
    led = idx.search_knowledge("LED current limit resistor")
    assert any(h["id"] == "led-series-resistor" for h in led)
    # trimmed payload: no raw book prose fields beyond statement
    assert all(set(h) <= {"id", "type", "statement", "source", "formula", "values", "erc_rule"} for h in led)
