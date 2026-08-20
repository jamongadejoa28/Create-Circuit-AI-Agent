"""Generic MCU/connector search uses KiCad library taxonomy, not FTS keyword hits."""

from circuitgen.agent import (
    Agent,
    _generic_connector_query,
    _generic_header_hits,
    _generic_mcu_query,
)


class _Catalog:
    def __init__(self, mapping):
        self.mapping = mapping

    def search_parts(self, query, limit=5):
        key = query.strip()
        return list(self.mapping.get(key, self.mapping.get(key.lower(), [])))[:limit]


def test_generic_mcu_query_strips_voltage_and_rejects_part_numbers():
    assert _generic_mcu_query({"search_query": "MCU"})
    assert _generic_mcu_query({"search_query": "3.3V MCU"})
    assert _generic_mcu_query({"search_query": "microcontroller"})
    assert not _generic_mcu_query({"search_query": "MC68332"})
    assert not _generic_mcu_query({"search_query": "STM32G474RET6"})
    assert not _generic_mcu_query({"search_query": "STM32 MCU"})


def test_generic_mcu_candidates_drop_cpu_library_keyword_hits():
    need = {"role": "mcu", "search_query": "MCU"}
    hits = [
        {"lib_id": "CPU_NXP_68000:MC68332", "description": "MCU 32 bit", "keywords": "MCU 32 bit"},
        {"lib_id": "MCU_NXP_ColdFire:MCF5212CAE66", "description": "ColdFire Microcontroller", "keywords": "MCU"},
        {"lib_id": "MCU_ST_STM32G4:STM32G474RETx", "description": "Arm Cortex-M4 MCU", "keywords": "STM32G4"},
    ]
    kept = Agent._filter_incompatible_candidates(need, hits)
    assert [h["lib_id"] for h in kept] == [hits[1]["lib_id"], hits[2]["lib_id"]]


def test_named_cpu_mcu_query_is_not_taxonomy_filtered():
    need = {"role": "mcu", "search_query": "MC68332"}
    hits = [
        {"lib_id": "CPU_NXP_68000:MC68332", "description": "MCU 32 bit", "keywords": "MCU 32 bit"},
        {"lib_id": "MCU_ST_STM32G4:STM32G474RETx", "description": "Arm Cortex-M4 MCU", "keywords": "STM32G4"},
    ]
    assert Agent._filter_incompatible_candidates(need, hits) == hits


def test_mcu_hits_whose_description_covers_the_rail_rank_first():
    from circuitgen.agent import _rank_mcu_hits_for_rail

    hits = [
        {
            "lib_id": "MCU_NXP_ColdFire:MCF5212CAE66",
            "description": "ColdFire Microcontroller, LQFP64",
            "pins": 64,
        },
        {
            "lib_id": "MCU_ST_STM32G4:STM32G474RETx",
            "description": "Arm Cortex-M4 MCU, 512KB flash, 1.71-3.6V, LQFP64",
            "pins": 64,
        },
        {
            "lib_id": "MCU_X:FIVE",
            "description": "MCU 4.5-5.5V",
            "pins": 40,
        },
    ]
    ranked = _rank_mcu_hits_for_rail(hits, 3.3)
    assert ranked[0]["lib_id"].endswith("STM32G474RETx")
    assert ranked[-1]["lib_id"].endswith("FIVE")


def test_generic_connector_query_rejects_named_families():
    assert _generic_connector_query({"search_query": "connector"})
    assert _generic_connector_query({"search_query": "header"})
    assert _generic_connector_query({"search_query": "pin header"})
    assert _generic_connector_query({"search_query": "1x4 header"})
    assert _generic_connector_query({"search_query": "커넥터"})
    assert not _generic_connector_query({"search_query": "LEMO"})
    assert not _generic_connector_query({"search_query": "LEMO connector"})
    assert not _generic_connector_query({"search_query": "USB-C"})
    assert not _generic_connector_query({"search_query": "SWD connector"})


def test_generic_header_hits_drop_lemo_and_one_pin_symbols():
    hits = [
        {"lib_id": "Connector:LEMO4", "pins": 4},
        {"lib_id": "Connector_Generic:Conn_01x01", "pins": 1},
        {"lib_id": "Connector_Generic:Conn_01x02", "pins": 2},
        {"lib_id": "Connector_Generic:Conn_01x04", "pins": 4},
    ]
    kept = _generic_header_hits(hits)
    assert [h["lib_id"] for h in kept] == [
        "Connector_Generic:Conn_01x02",
        "Connector_Generic:Conn_01x04",
    ]


def test_ensure_mcu_role_adds_when_prompt_names_mcu_but_spec_has_flash_and_lemo():
    agent = object.__new__(Agent)
    agent.parts = _Catalog({
        "W25Q32JVSS": [{"lib_id": "Memory_Flash:W25Q32JVSS"}],
        "connector": [{"lib_id": "Connector:LEMO4"}],
        "MCU": [{"lib_id": "MCU_ST_STM32G4:STM32G474RETx"}],
    })
    spec = {
        "parts_needed": [
            {"role": "flash", "search_query": "W25Q32JVSS", "quantity": 1},
            {"role": "header", "search_query": "connector", "quantity": 1},
        ]
    }
    agent._ensure_mcu_role(
        "3.3V MCU에 W25Q32JVSS SPI 플래시를 연결하는 회로", spec
    )
    queries = [p["search_query"] for p in spec["parts_needed"]]
    assert "MCU" in queries
    assert queries[0] == "MCU"


def test_ensure_mcu_role_keeps_a_named_cpu_already_in_the_spec():
    agent = object.__new__(Agent)
    agent.parts = _Catalog({
        "MC68332": [{"lib_id": "CPU_NXP_68000:MC68332"}],
        "MCU": [{"lib_id": "MCU_ST_STM32G4:STM32G474RETx"}],
    })
    spec = {"parts_needed": [{"role": "mcu", "search_query": "MC68332", "quantity": 1}]}
    agent._ensure_mcu_role("3.3V MCU에 플래시를 연결", spec)
    assert [p["search_query"] for p in spec["parts_needed"]] == ["MC68332"]


def test_ensure_mcu_role_silent_when_prompt_does_not_name_an_mcu():
    agent = object.__new__(Agent)
    agent.parts = _Catalog({
        "W25Q32JVSS": [{"lib_id": "Memory_Flash:W25Q32JVSS"}],
    })
    spec = {"parts_needed": [{"role": "flash", "search_query": "W25Q32JVSS", "quantity": 1}]}
    agent._ensure_mcu_role("W25Q32JVSS SPI 플래시만 있는 보드", spec)
    assert [p["search_query"] for p in spec["parts_needed"]] == ["W25Q32JVSS"]
