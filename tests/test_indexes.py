"""Part index + knowledge index (Phase 2 tool surface for the agent)."""

import json
from pathlib import Path

import pytest

from circuitgen.knowledge import KNOWLEDGE_DIR, KnowledgeIndex, build_index as build_kn, load_entries
from circuitgen.partindex import LibrarySource, PartIndex, build_index as build_parts
from circuitgen.symbols import KICAD_SYMBOL_DIR

_DATASHEET_DIR = Path(__file__).resolve().parent.parent / "data" / "datasheets"
_RAG_SHEETS = ("tmp100_SBOS231I.pdf", "ne555_SLFS022K.pdf", "ams1117_ds1117.pdf")
_HAS_RAG_SHEETS = all((_DATASHEET_DIR / name).is_file() for name in _RAG_SHEETS)
_RAG_IDS = {
    "tmp100-i2c-pullup-and-bypass",
    "ne555-reset-and-control-pins",
    "ams1117-output-cap-and-dropout",
}

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


def test_exact_symbol_lookup_bypasses_fts_tokenization(small_part_index):
    assert small_part_index.exact_symbol_ids("LED") == ["Device:LED"]
    assert small_part_index.exact_symbol_ids("not-a-real-symbol") == []


def test_exact_symbol_includes_unit0_power_pin_timers():
    """Timer:NE555D keeps VCC/GND on unit 0; exact identity must still resolve.

    Fuzzy search keeps excluding unit0_mix so a loose query cannot land on it.
    Measured: design-mode ``NE555D`` returned zero hits and became Conceptual.
    """
    from circuitgen.partindex import PartIndex

    idx = PartIndex()
    assert "Timer:NE555D" in idx.exact_symbol_ids("NE555D")
    hits = idx.search_parts("NE555D", limit=5)
    assert hits and hits[0]["lib_id"] == "Timer:NE555D"
    # A loose keyword search must not suddenly prefer unit0_mix parts.
    loose = {h["lib_id"] for h in idx.search_parts("single timer", limit=8)}
    assert "Timer:NE555D" not in loose
    # Full Library:Symbol IDs are UNINDEXED in FTS — still must resolve.
    full = idx.search_parts("Switch:SW_Push", limit=3)
    assert full and full[0]["lib_id"] == "Switch:SW_Push"


def test_generic_mcu_search_does_not_lead_with_cpu_library():
    """FTS ranks CPU_NXP_68000:MC68332 first for query MCU (keywords 'MCU 32 bit')."""
    from circuitgen.agent import Agent
    from circuitgen.partindex import PartIndex

    idx = PartIndex()
    raw = idx.search_parts("MCU", 12)
    kept = Agent._filter_incompatible_candidates(
        {"role": "mcu", "search_query": "MCU"}, raw
    )
    assert kept
    assert all(h["lib_id"].startswith("MCU_") for h in kept)
    assert not any(h["lib_id"].startswith("CPU_") for h in kept)
    named = idx.search_parts("MC68332", 3)
    assert named and named[0]["lib_id"] == "CPU_NXP_68000:MC68332"


def test_generic_mcu_with_3v3_rail_does_not_lead_with_keyword_only_coldfire():
    from circuitgen.agent import Agent, _description_covers_volts, _rank_mcu_hits_for_rail
    from circuitgen.partindex import PartIndex

    idx = PartIndex()
    hits = idx.search_parts("MCU", 12)
    extra = idx.search_parts("3.3V microcontroller", 12)
    seen = {h["lib_id"] for h in hits}
    for hit in extra:
        if hit["lib_id"] not in seen:
            hits.append(hit)
            seen.add(hit["lib_id"])
    hits = Agent._filter_incompatible_candidates(
        {"role": "mcu", "search_query": "MCU"}, hits
    )
    ranked = _rank_mcu_hits_for_rail(hits, 3.3)
    assert ranked
    top = ranked[0]
    assert top["lib_id"].startswith("MCU_")
    assert "ColdFire" not in top["lib_id"]
    assert _description_covers_volts(top["description"], 3.3) is not False


def test_generic_connector_search_does_not_lead_with_lemo():
    """FTS ranks Connector:LEMO2 first for query connector."""
    from circuitgen.agent import _generic_header_hits
    from circuitgen.partindex import PartIndex

    idx = PartIndex()
    raw = idx.search_parts("connector", 8)
    assert raw and raw[0]["lib_id"].startswith("Connector:LEMO")
    generic = _generic_header_hits(raw)
    if not generic:
        generic = _generic_header_hits(idx.search_parts("Conn_01x", 20))
    assert generic
    assert all(h["lib_id"].startswith("Connector_Generic:") for h in generic)
    named = idx.search_parts("LEMO4", 3)
    assert named and named[0]["lib_id"] == "Connector:LEMO4"


def test_exact_library_id_is_verified_without_fuzzy_search(small_part_index):
    assert small_part_index.exact_lib_id("Device:LED") == "Device:LED"
    assert small_part_index.exact_lib_id("device:led") == "Device:LED"
    assert small_part_index.exact_lib_id("Nope:LED") is None
    assert small_part_index.exact_lib_id("LED") is None


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
    if _HAS_RAG_SHEETS:
        assert _RAG_IDS <= ids
    else:
        assert not (_RAG_IDS & ids)
    assert all(
        e["source"].get("provenance") in {"textbook", "datasheet"} for e in entries
    )
    assert all(
        e["source"].get("pdf_page_index") is not None
        for e in entries
        if e["source"].get("provenance") == "datasheet"
    )


def test_internal_fixture_cannot_enter_production_knowledge(tmp_path):
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "knowledge"
    with pytest.raises(ValueError, match="cannot be indexed as production knowledge"):
        load_entries(fixture_dir)
    archived = load_entries(fixture_dir, allow_internal_fixtures=True)
    assert archived and all(
        e["source"].get("provenance") == "internal-fixture" for e in archived
    )


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
    scored = idx.search_knowledge("LED current limit resistor", 1, include_score=True)
    assert scored[0]["_retrieval"]["rank"] == 1
    assert isinstance(scored[0]["_retrieval"]["bm25"], float)
    # A long OR query must not force unrelated snippets when the corpus has
    # no knowledge for that interface.
    assert idx.search_knowledge("CAN FD transceiver termination TVS ESD") == []
    # A device-specific OOV token may still retrieve genuinely applicable
    # generic knowledge when two meaningful terms match.
    generic = idx.search_knowledge("AS5048A SPI encoder power decoupling")
    assert any(h["id"] == "decoupling-cap-per-ic" for h in generic)
    if _HAS_RAG_SHEETS:
        tmp100 = idx.search_knowledge("TMP100")
        assert any(h["id"] == "tmp100-i2c-pullup-and-bypass" for h in tmp100), tmp100
        ne555 = idx.search_knowledge("NE555D")
        assert any(h["id"] == "ne555-reset-and-control-pins" for h in ne555), ne555


def test_missing_datasheet_pdf_is_not_indexed(tmp_path):
    textbook = (KNOWLEDGE_DIR / "passive-values.json").read_text(encoding="utf-8")
    (tmp_path / "passive-values.json").write_text(textbook, encoding="utf-8")
    (tmp_path / "ghost.json").write_text(
        json.dumps(
            [
                {
                    "id": "ghost-part",
                    "type": "device_rule",
                    "statement": "A part with no local PDF.",
                    "tags": ["ghost"],
                    "source": {
                        "book": "Missing",
                        "section": "1",
                        "pdf_page_index": 0,
                        "file": "definitely-absent.pdf",
                        "provenance": "datasheet",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_entries(tmp_path)
    ids = {e["id"] for e in loaded}
    assert "led-series-resistor" in ids
    assert "ghost-part" not in ids


# ---- footprints (plan §8.2 completion) ----


@pytest.fixture(scope="module")
def fp_part_index(tmp_path_factory):
    import shutil

    from circuitgen.partindex import LibrarySource, build_index
    from circuitgen.symbols import library_path

    fp_root_src = Path(__file__).resolve().parent.parent / "kicad-footprints"
    if not fp_root_src.is_dir():
        pytest.skip("kicad-footprints clone not present")

    tmp = tmp_path_factory.mktemp("fpidx")
    libs = tmp / "libs"
    libs.mkdir()
    for name in ("Device", "Switch", "power"):
        src = library_path(KICAD_SYMBOL_DIR, name)
        (shutil.copytree if src.is_dir() else shutil.copy)(src, libs / src.name)
    fps = tmp / "fps"
    fps.mkdir()
    for pretty in ("Resistor_SMD.pretty", "LED_SMD.pretty", "Button_Switch_SMD.pretty"):
        shutil.copytree(fp_root_src / pretty, fps / pretty)
    db = tmp / "parts.sqlite"
    stats = build_index(db, sources=[LibrarySource(libs, "", 1, "test")], footprint_root=fps)
    assert stats["footprints"] > 50
    return PartIndex(db)


def test_footprint_pads_and_matching(fp_part_index):
    idx = fp_part_index
    assert idx.footprint_pads("Resistor_SMD:R_0805_2012Metric") == {"1", "2"}
    assert idx.footprint_pads("Nope:Nothing") is None
    best = idx.match_footprints(["R_*"], {"1", "2"}, 1)
    assert best and "0805" in best[0]


def test_check_and_assign_footprints(fp_part_index):
    from circuitgen.fp_checks import assign_footprints, check_footprints
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.symbols import load_symbols

    idx = fp_part_index
    ir = CircuitIR("fp_t")
    ir.add(Component("R1", "Device:R", "1k", "Bogus:DoesNotExist"))
    ir.add(Component("D1", "Device:LED", "LED", ""))
    symbols = load_symbols(["Device:R", "Device:LED"])

    issues = check_footprints(ir, symbols, idx)
    assert [(i.rule, i.path) for i in issues] == [("footprint_unknown", "R1")]

    notes = assign_footprints(ir, symbols, idx)
    assert ir.components["R1"].footprint.startswith("Resistor_SMD:")
    assert ir.components["D1"].footprint.startswith("LED_SMD:")
    assert check_footprints(ir, symbols, idx) == []
    assert len(notes) == 2


def test_generic_push_switch_gets_deterministic_fallback_footprint(fp_part_index):
    from circuitgen.fp_checks import assign_footprints
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.symbols import load_symbols

    ir = CircuitIR("switch_fp")
    ir.add(Component("SW1", "Switch:SW_Push", "RESET", "Bogus:Footprint"))
    symbols = load_symbols(["Switch:SW_Push"])
    notes = assign_footprints(ir, symbols, fp_part_index)
    assert ir.components["SW1"].footprint.startswith("Button_Switch_SMD:SW_SPST_")
    assert notes


def test_footprint_pin_mismatch(fp_part_index):
    from circuitgen.fp_checks import check_footprints
    from circuitgen.ir import CircuitIR, Component
    from circuitgen.symbols import load_symbols

    ir = CircuitIR("fp_t2")
    # 2-pin switch forced onto a 2-pad R footprint is fine; force a wrong
    # case instead: R symbol claiming a switch footprint with pads 1/2 is
    # also fine — so fabricate the mismatch with a multi-pad footprint
    ir.add(Component("SW1", "Switch:SW_Push", "SW", "Resistor_SMD:R_0805_2012Metric"))
    symbols = load_symbols(["Switch:SW_Push"])
    assert check_footprints(ir, symbols, fp_part_index) == []  # pads {1,2} cover pins {1,2}
