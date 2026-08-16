"""Requested connector contact counts vs the symbol and footprint that landed.

These cases use header notation (1x2, 1x4, 2x3, N-pin), not a campaign prompt.
"""

from circuitgen.compliance import check_compliance, check_connector_geometry
from circuitgen.fp_checks import parse_contact_geometry
from circuitgen.ir import CircuitIR, Component, PinDef, SymbolDef
from circuitgen.pins import PinType


def _header(lib_id, pins):
    return SymbolDef(
        lib_id, f'(symbol "{lib_id.split(":")[-1]}")',
        [PinDef(str(n), str(n), PinType.PASSIVE, 0, 0, 0, 2.54) for n in range(1, pins + 1)],
        reference_prefix="J",
    )


class _Pads:
    def __init__(self, pads):
        self._pads = pads

    def has_footprints(self):
        return True

    def footprint_pads(self, _fp):
        return set(self._pads)


def _board(lib_id, pin_count, footprint="Connector_PinHeader_2.54mm:PinHeader_1x04"):
    ir = CircuitIR("geom")
    ir.add(Component("J1", lib_id, "", footprint))
    symbols = {lib_id: _header(lib_id, pin_count)}
    return ir, symbols


def test_parse_requires_connector_words():
    assert parse_contact_geometry("1x2 pin header") == {
        "rows": 1, "columns": 2, "contacts": 2,
    }
    assert parse_contact_geometry("1x4 connector")["contacts"] == 4
    assert parse_contact_geometry("2x3 pin header") == {
        "rows": 2, "columns": 3, "contacts": 6,
    }
    assert parse_contact_geometry("6-pin header") == {
        "rows": 1, "columns": 6, "contacts": 6,
    }
    assert parse_contact_geometry("4핀 커넥터")["contacts"] == 4
    assert parse_contact_geometry("AMS1117-3.3") is None
    assert parse_contact_geometry("SOT-223") is None
    assert parse_contact_geometry("header") is None


def test_generic_header_lib_id_follows_kicad_catalog_names():
    from circuitgen.fp_checks import generic_header_lib_id

    assert generic_header_lib_id({"rows": 1, "columns": 2, "contacts": 2}) == (
        "Connector_Generic:Conn_01x02"
    )
    assert generic_header_lib_id({"rows": 1, "columns": 4, "contacts": 4}) == (
        "Connector_Generic:Conn_01x04"
    )
    assert generic_header_lib_id({"rows": 2, "columns": 3, "contacts": 6}) == (
        "Connector_Generic:Conn_02x03_Odd_Even"
    )


def test_one_by_two_rejects_four_pin_symbol():
    ir, symbols = _board("Connector_Generic:Conn_01x04", 4)
    spec = {"parts_needed": [{
        "role": "power_in", "search_query": "1x2 pin header",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, records = check_connector_geometry(ir, symbols, spec)
    assert issues and issues[0].rule == "connector_contact_geometry"
    assert records[0]["requested_contacts"] == 2
    assert records[0]["symbol_pins"] == 4
    assert records[0]["match"] is False


def test_one_by_four_accepts_four_pin_symbol():
    ir, symbols = _board("Connector_Generic:Conn_01x04", 4)
    spec = {"parts_needed": [{
        "role": "debug", "search_query": "1x4 pin header",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, records = check_connector_geometry(ir, symbols, spec)
    assert issues == []
    assert records[0]["match"] is True


def test_two_by_three_counts_six_contacts():
    ir, symbols = _board("Connector_Generic:Conn_02x03", 6, "F:2x3")
    spec = {"parts_needed": [{
        "role": "isp", "search_query": "2x3 connector",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, records = check_connector_geometry(ir, symbols, spec)
    assert issues == []
    assert records[0]["requested_contacts"] == 6
    assert records[0]["symbol_pins"] == 6


def test_n_pin_notation_matches_pin_count():
    ir, symbols = _board("Connector_Generic:Conn_01x06", 6)
    spec = {"parts_needed": [{
        "role": "io", "search_query": "6-pin header",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, _records = check_connector_geometry(ir, symbols, spec)
    assert issues == []


def test_n_pin_mismatch_and_footprint_pad_count():
    ir, symbols = _board("Connector_Generic:Conn_01x04", 4, "F:4pad")
    spec = {"parts_needed": [{
        "role": "io", "search_query": "6-pin connector",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, records = check_connector_geometry(
        ir, symbols, spec, parts=_Pads({"1", "2", "3", "4"}),
    )
    assert issues
    assert records[0]["symbol_pins"] == 4
    assert records[0]["footprint_pads"] == 4
    assert records[0]["requested_contacts"] == 6


def test_quantity_two_reports_a_missing_second_header():
    ir, symbols = _board("Connector_Generic:Conn_01x02", 2, "F:1x2")
    spec = {"parts_needed": [{
        "role": "header", "search_query": "1x2 pin header",
        "functional_kind": "connector", "quantity": 2,
    }]}
    issues, records = check_connector_geometry(ir, symbols, spec)
    assert len(records) == 2
    assert records[0]["match"] is True
    assert records[0]["symbol_pins"] == 2
    assert records[1]["match"] is False
    assert records[1]["symbol_pins"] is None
    assert any(i.rule == "connector_contact_geometry" for i in issues)


def test_no_geometry_is_not_a_mismatch():
    ir, symbols = _board("Connector_Generic:Conn_01x04", 4)
    spec = {"parts_needed": [{
        "role": "io", "search_query": "header",
        "functional_kind": "connector", "reference": "J1",
    }]}
    issues, records = check_connector_geometry(ir, symbols, spec)
    assert issues == []
    assert records == []


def test_compliance_report_includes_connector_geometry():
    ir, symbols = _board("Connector_Generic:Conn_01x04", 4)
    ir.add(Component("#PWR01", "power:+5V", "+5V"))
    spec = {
        "power": {"rails": [{"name": "+5V", "voltage": "5"}]},
        "parts_needed": [{
            "role": "power_in", "search_query": "1x2 pin header",
            "functional_kind": "connector", "reference": "J1",
        }],
    }
    report = check_compliance(ir, symbols, spec=spec)
    assert report.connector_geometry
    assert report.connector_geometry[0]["match"] is False
    assert any(i.rule == "connector_contact_geometry" for i in report.issues)
