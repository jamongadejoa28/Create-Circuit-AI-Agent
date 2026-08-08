# CircuitGen RAG 및 전체 시스템 아키텍처

작성일: 2026-08-08  
대상 코드: `/home/hajun/dev_ws/create_circuit`

> 상태 갱신(2026-08-08): 이 문서의 "34개"는 최초 기준선 설명이다. 이후
> Tier 2~4 지식 62개가 추가되어 현재 인덱스는 96개다. §14에 확장 결과와
> 검색 평가·재현성 기록 구현을 추가했다.

## 1. 이 문서의 목적

이 문서는 다음을 명확히 설명한다.

1. 현재 말하는 "34개 규칙"이 실제로 무엇인지
2. 어느 파일에 저장되고 어떤 코드가 읽는지
3. 검색된 규칙이 회로 생성 과정에서 언제, 어떻게 작용하는지
4. RAG 지식과 결정론적 Python 회로 규칙의 차이
5. 사용자 프롬프트부터 KiCad 산출물까지 전체 실행 흐름
6. 현재 아키텍처의 한계와 LlamaIndex를 도입할 위치

## 2. 34개 규칙의 정확한 의미

### 2.1 코드에 하드코딩된 실행 규칙이 아니다

현재 34개 항목은 `if/else`로 실행되는 ERC 또는 회로 생성 코드가 아니다. 전공서적 등에서 추출하고 사람이 검수한 짧은 설계 지식 엔트리다. 각 엔트리는 JSON 객체이며 다음과 같은 정보를 가진다.

```json
{
  "id": "decoupling-cap-per-ic",
  "type": "component_rule",
  "statement": "...",
  "condition": "...",
  "tags": ["decoupling", "capacitor"],
  "source": {
    "book": "...",
    "section": "..."
  }
}
```

이 엔트리 중 사용자 요구사항과 어휘 검색이 일치한 일부만 Qwen 프롬프트의 `KNOWLEDGE` 필드로 전달된다. Qwen에게 설계 참고 근거를 주는 것이므로 적용 여부는 모델 출력에 의존한다.

### 2.2 원본 데이터 파일과 개수

| 파일 | 개수 | 주요 내용 |
|---|---:|---|
| `data/knowledge/digital-power-practice.json` | 5 | 디커플링, 풀업/풀다운, 미사용 입력 |
| `data/knowledge/floyd-digital.json` | 7 | 555, TTL fan-out, 논리 레벨, 미사용 입력 |
| `data/knowledge/passive-values.json` | 3 | E12/E24, LED 직렬 저항 |
| `data/knowledge/sadiku-opamp-filters.json` | 9 | op-amp와 수동/능동 필터 공식 |
| `data/knowledge/schematic-conventions.json` | 2 | 회로도 흐름과 annotation 관례 |
| `data/knowledge/sedra-analog.json` | 8 | 정류기, ripple, BJT/MOSFET bias, 발진기, 555 |
| 합계 | **34** | 검수된 설계 지식 엔트리 |

유형별로는 `formula` 16개, `component_rule` 10개, `table` 5개, `convention` 2개, `example` 1개다.

### 2.3 관련 코드 파일

| 역할 | 파일 |
|---|---|
| JSON 로딩과 스키마 검증 | `src/circuitgen/knowledge.py::load_entries()` |
| SQLite FTS5 인덱스 생성 | `src/circuitgen/knowledge.py::build_index()` |
| 질의를 FTS5 OR 표현식으로 변환 | `src/circuitgen/knowledge.py::_fts_query()` |
| BM25 검색과 결과 축약 | `src/circuitgen/knowledge.py::KnowledgeIndex.search_knowledge()` |
| 인덱스 빌드 명령 | `scripts/build_knowledge_index.py` |
| 검색 topic 생성 및 결과 수집 | `src/circuitgen/agent.py::Agent._gather()` |
| 단일 회로 합성 프롬프트 주입 | `src/circuitgen/agent.py::Agent.synthesize_ir()` |
| 블록별 합성 프롬프트 주입 | `src/circuitgen/agent.py::Agent.synthesize_block()` |
| 검색 회귀 테스트 | `tests/test_indexes.py::test_knowledge_search()` |

빌드 명령은 다음과 같다.

```bash
PYTHONPATH=src .venv/bin/python scripts/build_knowledge_index.py
```

이 명령은 JSON 34개를 읽고 `data/knowledge.sqlite`를 다시 만든다. 현재 DB 크기는 약 116KB다.

## 3. 34개 지식이 실제로 작용하는 과정

```text
data/knowledge/*.json
        │ build_knowledge_index.py
        ▼
data/knowledge.sqlite
  ├─ entries: 원본 JSON 보존
  └─ entries_fts: statement/condition/tags/type 검색
        │
        │ KnowledgeIndex.search_knowledge(topic, limit=2)
        ▼
Agent._gather()
  ├─ 부품별 search_query
  ├─ connections_intent 앞 4개
  ├─ 중복 ID 제거
  └─ 최대 6개 snippet으로 절단
        │
        ▼
synthesize_ir() 또는 synthesize_block()
        │
        ├─ SPEC
        ├─ CANDIDATES
        ├─ PIN_TABLES
        └─ KNOWLEDGE  ← 이 위치에 검색 결과 삽입
        │
        ▼
Qwen2.5-Coder JSON 생성
        │
        ▼
CircuitIR
```

### 3.1 검색 topic 생성

`Agent._gather()`는 다음 문자열을 검색어로 사용한다.

```python
topics = [n["search_query"] for n in spec["parts_needed"]]
topics += spec["connections_intent"][:4]
```

예를 들어 요구사항 분석 결과가 다음과 같다면:

```json
{
  "parts_needed": [
    {"role": "encoder", "search_query": "AS5048A"},
    {"role": "driver", "search_query": "BLDC motor driver"}
  ],
  "connections_intent": [
    "SPI encoder with pull-up and series resistor"
  ]
}
```

검색 topic은 `AS5048A`, `BLDC motor driver`, `SPI encoder with pull-up and series resistor`가 된다.

### 3.2 검색과 개수 제한

각 topic마다 최대 2개(`KNOWLEDGE_PER_TOPIC = 2`)를 가져온다. 같은 지식 ID는 한 번만 유지하고, 최종적으로 처음 발견된 6개만 사용한다.

```python
for topic in topics:
    hits = search_knowledge(topic, 2)
    ...
snippets = snippets[:6]
```

현재는 모든 topic 결과를 하나의 전역 관련도 점수로 재정렬하지 않는다. 따라서 `parts_needed` 순서가 최종 6개 선택에 영향을 준다.

### 3.3 Qwen 프롬프트에 전달되는 필드

전체 JSON 엔트리를 전달하지 않고 컨텍스트 절약을 위해 다음 필드만 전달한다.

- `id`
- `type`
- `statement`
- `source`
- 선택적으로 `formula`, `values`, `erc_rule`

Qwen 합성 프롬프트에는 `Apply the KNOWLEDGE rules`라는 지시와 함께 이 배열이 들어간다. 하지만 모델이 반드시 적용했다는 보장은 없으며, 현재는 어떤 검색 결과가 실제 회로 요소로 반영됐는지 추적하는 provenance 연결도 없다.

### 3.4 언제 사용되지 않는가

34개 지식은 다음 단계에서는 직접 사용되지 않는다.

- 요구사항 추출
- 블록 계획 생성
- CircuitIR 병합
- 자체 ERC
- KiCad ERC
- 배치와 SVG 시각 검사
- 자동 repair 프롬프트

특히 repair 단계는 현재 ERC 문제와 후보 부품만 전달하며 최초에 검색했던 `KNOWLEDGE`를 다시 전달하지 않는다.

## 4. RAG 지식과 결정론적 회로 규칙의 차이

두 계층을 혼동하면 안 된다.

| 구분 | RAG 지식 | 결정론적 Python 규칙 |
|---|---|---|
| 저장 위치 | `data/knowledge/*.json` | `src/circuitgen/*.py` |
| 적용 주체 | Qwen | Python 코드 |
| 적용 보장 | 없음 | 조건 충족 시 항상 적용 |
| 예 | “IC마다 디커플링을 둔다”는 문장 | DRV8311마다 네 종류 C를 실제 생성 |
| 검증 방식 | 프롬프트/결과 관찰 | 단위 테스트 |
| 실패 형태 | 무시, 오해, 누락 | 코드 버그 또는 규칙 미지원 |

현재 주요 결정론적 규칙은 다음과 같다.

| 규칙 | 코드 |
|---|---|
| I²C/CS 풀업 자동 삽입 | `normalize.py::ensure_bus_pullups()` |
| power-in net에 PWR_FLAG 삽입 | `normalize.py::ensure_pwr_flags()` |
| STM32/AS5048A/DRV8311/TJA1051의 알려진 전원·NC 처리 | `normalize.py::complete_known_device_pins()` |
| 공유 AS5048A MISO에 47Ω 직렬 저항 | `normalize.py::add_shared_spi_miso_series_resistors()` |
| DRV8311 VM에 100nF/1uF/10uF/220uF | `normalize.py::ensure_drv8311_vm_decoupling()` |
| 반복 블록 수량과 신호 namespacing | `blocks.py::validate_plan()`, `instantiate_blocks()` |
| 실제 핀 번호 해석 | `agent.py::resolve_pin_names()` |
| 풋프린트 자동 선택/검사 | `fp_checks.py` |

회로 제작 신뢰도를 높이는 핵심은 고가치 RAG 문장을 점차 검수된 결정론적 `DeviceRule`로 승격하는 것이다.

## 5. 전체 시스템 구성요소

### 5.1 실행 환경

```text
WSL2 / Python
  ├─ CircuitGen Agent
  ├─ SQLite part/knowledge index
  ├─ CircuitIR 및 EDA 생성기
  └─ 테스트와 결과 분석

Windows
  ├─ llama.cpp server : Qwen2.5-Coder-7B
  └─ KiCad 10 CLI     : ERC/netlist/SVG 검증
```

### 5.2 주요 계층

```text
┌─────────────────────────────────────────────────────────────┐
│ 1. 입력/오케스트레이션                                      │
│ scripts/run_agent.py, webapp.py, agent.py                   │
├─────────────────────────────────────────────────────────────┤
│ 2. 모델 계층                                                │
│ llm_client.py → Windows llama.cpp → Qwen                    │
├─────────────────────────────────────────────────────────────┤
│ 3. 검색 계층                                                │
│ partindex.py         knowledge.py                           │
│ KiCad 부품/핀/FP     34개 설계 지식 FTS5                    │
├─────────────────────────────────────────────────────────────┤
│ 4. 구조화 회로 계층                                         │
│ schemas.py, ir.py, ir_json.py, blocks.py                    │
├─────────────────────────────────────────────────────────────┤
│ 5. 결정론적 보정/검증                                       │
│ normalize.py, erc.py, fp_checks.py, visual.py               │
├─────────────────────────────────────────────────────────────┤
│ 6. EDA 출력 계층                                            │
│ place.py, emit.py, netlist.py, project.py                   │
├─────────────────────────────────────────────────────────────┤
│ 7. 외부 검증 계층                                           │
│ kicad_cli.py → KiCad ERC / SVG / netlist                    │
├─────────────────────────────────────────────────────────────┤
│ 8. 감사/산출물                                               │
│ audit.py → run.json, out/agent/*.kicad_sch                  │
└─────────────────────────────────────────────────────────────┘
```

## 6. 사용자 프롬프트부터 KiCad 산출물까지

### 6.1 진입점

CLI 실행 예:

```bash
PYTHONPATH=src .venv/bin/python scripts/run_agent.py "회로 요구사항" bldc_v14
```

`run_agent.py`는 다음 객체를 구성한다.

```python
Agent(
    LlamaClient(),
    PartIndex(),
    KnowledgeIndex(),
    out_dir,
)
```

### 6.2 단계별 실행 흐름

```text
[사용자 프롬프트]
       │
       ▼
1. RequirementSpec 생성
   Agent.extract_requirements()
   Qwen + REQUIREMENT_SPEC JSON Schema
       │
       ├─ rail 이름 정규화
       ├─ role 중복 제거
       └─ 명시 부품번호 누락 보완
       │
       ▼
2. 요구사항 승인/범위 검사
   max 24VDC / 3A 등 현재 정책
       │
       ▼
3. 소형/대형 회로 분기
   parts_needed < 5  ────────────┐
   parts_needed >= 5             │
       │                         │
       ▼                         ▼
4A. 블록 계획                4B. 단일 합성
    plan_blocks()                synthesize_ir()
    Qwen + BLOCK_PLAN               │
       │                            │
       ▼                            │
5A. 블록별 검색과 합성              │
    PartIndex + KnowledgeIndex       │
    synthesize_block()               │
       │                            │
       ▼                            │
6A. 반복 블록 복제/병합              │
    instantiate_blocks()             │
    MCU interface 연결               │
       └──────────────┬─────────────┘
                      ▼
7. CircuitIR 정규화
   pin 이름→번호, rail, 전원 심볼, 풀업, footprint
   장치별 전원/NC, MISO R, DRV8311 VM C
                      │
                      ▼
8. deterministic pipeline
   자체 ERC → 배치 → KiCad schematic emit
                      │
                      ▼
9. KiCad 외부 검증
   KiCad ERC → SVG → netlist round-trip
                      │
            실패      │      성공
          ┌───────────┴───────────┐
          ▼                       ▼
10. 최대 3회 patch repair       완료
    Qwen + REPAIR_PATCH          run.json 저장
          │
          └──── 다시 8번으로
```

## 7. 각 데이터 구조의 역할

### 7.1 RequirementSpec

자연어 요구사항을 구조화한 중간 결과다. 주요 필드:

- 요약
- 전원 rail
- 필요한 부품 역할, 검색어, 수량
- 연결 의도
- 범위 초과 여부

스키마는 `src/circuitgen/schemas.py::REQUIREMENT_SPEC`에 있다.

### 7.2 PartIndex

`data/parts.sqlite`에 있는 KiCad 구조화 부품 DB다. 주요 기능:

- `search_parts(query)`
- `get_part_pins(lib_id)`
- `load_symbols(lib_ids)`
- footprint pad 조회와 매칭
- source/license/checksum provenance

부품 ID와 핀 번호는 회로 연결의 정확성에 직접 관여하므로 일반 문서 RAG와 분리되어 있다.

### 7.3 BlockPlan

보드 규모 요구사항을 작은 기능 블록으로 분해한다.

- block ID
- 역할 목록
- 반복 count
- interface net
- 설명

`blocks.py`는 모델 계획을 검증하고 반복 수량, per-instance net, passive-only block 문제를 결정론적으로 보정한다.

### 7.4 CircuitIR

LLM과 KiCad 사이의 핵심 중간 표현이다.

```text
CircuitIR
  ├─ name
  ├─ components[ref]
  │    ├─ lib_id
  │    ├─ value
  │    ├─ footprint
  │    └─ group
  ├─ nets[]
  │    ├─ name
  │    └─ nodes[(ref, pin)]
  └─ nc_pins[(ref, pin)]
```

CircuitIR은 `ir.py`에 정의되고 `ir_json.py`가 JSON 변환과 repair patch를 담당한다.

## 8. 결정론적 검증 파이프라인

`pipeline.py::generate()`는 LLM을 호출하지 않는다.

1. conceptual symbol 해석
2. 필요한 PWR_FLAG 삽입
3. 자체 ERC
4. footprint 검사
5. 기능 그룹 기반 배치
6. 도면 경계/심볼 겹침 시각 QA
7. KiCad 10 S-expression 생성
8. KiCad CLI ERC 실행
9. SVG export
10. KiCad netlist export
11. CircuitIR과 KiCad netlist 연결 동등성 비교

자체 ERC 오류가 있어도 알려진 심볼만으로 draft 회로도를 생성한다. 이는 불완전한 보드도 사람이 이미지로 확인할 수 있게 하기 위한 정책이다.

## 9. 자동 repair 루프

파이프라인이 실패하면 최대 3회 repair를 수행한다.

```text
self ERC/visual/pipeline error
        │
        ▼
문제 주변의 축약된 CircuitIR
        │
        ▼
Qwen REPAIR_PATCH JSON
        │
        ▼
허용 연산 필터링
        │
        ▼
IR patch 적용 → 재검증
```

동일 문제가 두 번 반복되면 조기 중단한다. 알려진 유효 부품을 모델이 임의로 교체하는 patch 등은 gate에서 거부한다.

## 10. 산출물과 감사 기록

주요 산출물:

- `out/agent/<name>.kicad_sch`
- `out/agent/<name>.kicad_pro`
- `out/agent/<name>.erc.json`
- `out/agent/<name>.net`
- `out/agent/<name>.kicad-export.net`
- `out/agent/svg/<name>.svg`
- `out/agent/run.json`

`run.json`에는 prompt, spec, block plan, 최종 IR, repair 기록, 결과가 저장된다. 현재 검색된 knowledge snippet 자체는 감사 기록에 저장하지 않으므로, 정확한 RAG provenance 추적을 위해 향후 추가할 필요가 있다.

## 11. 현재 RAG 아키텍처의 주요 한계

### 11.1 BLDC 지식 공백

34개 항목은 기초 전자 규칙 위주다. DRV8311, STM32G474 AF/ADC, AS5048A, CAN-FD, 배터리 보호 관련 장치별 지식은 거의 없다. 검색 엔진을 바꿔도 없는 지식은 검색할 수 없다.

### 11.2 OR 검색 과매칭

현재 `_fts_query()`는 모든 공백 토큰을 OR로 연결한다. 긴 질의의 흔한 단어 하나만 맞아도 관련 없는 지식이 반환될 수 있다.

### 11.3 점수 임계값 부재

BM25 상위 N개를 무조건 사용한다. 관련 지식이 없을 때 빈 결과를 내는 정책이 약하다.

### 11.4 전역 reranking 부재

topic 순서대로 결과를 누적하고 앞의 6개를 사용하므로 전체 후보 중 가장 관련 있는 6개라는 보장이 없다.

### 11.5 적용 여부 추적 부재

어떤 knowledge ID가 어떤 component/net 생성의 근거가 되었는지 CircuitIR에 기록하지 않는다.

### 11.6 repair에서 지식 단절

초기 합성에 사용한 지식이 repair 프롬프트에 다시 들어가지 않아, repair가 최초 설계 근거를 훼손할 수 있다.

## 12. 권장 개선 아키텍처와 LlamaIndex 위치

```text
KnowledgeRetriever Protocol
          │
   ┌──────┴─────────┐
   ▼                ▼
SQLite FTS5      LlamaIndex Vector
exact/tag/BM25   multilingual semantic
   │                │
   └──── Hybrid/RRF ┘
          │
 metadata filter + threshold
          │
 검수된 KnowledgeHit
          │
 DeviceRuleResolver
          │
 결정론적 CircuitIR 변환
```

권장 순서:

1. 현재 FTS에 한영 alias, exact tag, 점수 임계값 추가
2. 검색 평가셋 구축
3. BLDC/STM32/AS5048A/CAN-FD 데이터시트 지식 추가
4. `KnowledgeRetriever` 인터페이스로 현재 구현 분리
5. LlamaIndex 벡터 검색을 선택적 백엔드로 추가
6. FTS와 vector를 hybrid fusion
7. 고가치 지식을 결정론적 `DeviceRule`로 승격

부품 검색인 `PartIndex`는 구조화 핀/footprint/라이선스 DB이므로 LlamaIndex로 교체하지 않는다.

## 13. 아키텍처 핵심 요약

- 34개 규칙은 `data/knowledge/*.json`에 있는 **검색용 설계 지식**이다.
- `knowledge.py`가 이를 SQLite FTS5로 인덱싱하고 검색한다.
- `agent.py::_gather()`가 관련 항목 일부를 선택한다.
- 선택된 항목은 Qwen의 최초 CircuitIR 합성 프롬프트에만 들어간다.
- 모델이 지식을 실제로 적용한다는 보장은 없다.
- `normalize.py`, `blocks.py`, `erc.py`의 Python 규칙은 별도의 **강제 실행 계층**이다.
- 최종 신뢰도는 RAG만이 아니라 구조화 IR, 장치별 결정론적 규칙, 자체 ERC, KiCad CLI 검증의 결합으로 확보해야 한다.

## 14. 96개 확장 이후 구현 상태

### 14.1 추가 지식

`data/knowledge/tier_*.json` 6개 파일에 62개가 추가되어 총 96개가 됐다.

- `circuit_pattern`: 21
- `device_rule`: 13
- `failure_mode`: 11
- `worked_design`: 9
- `selection_guidance`: 8
- 기존 formula/component_rule/table/convention/example: 34

### 14.2 보드 A/B 산출물

- 기준선: `out/bench_boards/coder-base.jsonl`과 `coder-base/boardXX/`
- 96개: `out/bench_boards/coder-kn96.jsonl`과 `coder-kn96/boardXX/`
- 실행기: `scripts/bench_boards.py`

단일 실행 결과는 가시 draft 17/18→18/18, 평균 KiCad 위반 185.1→138.9였다.
기준선에서 결과가 없던 board17을 제외한 paired 17보드 평균은 185.1→135.3이다.

### 14.3 재현성 및 RAG provenance

새 실행의 `run.json`은 다음을 추가 기록한다.

- 모델 ID와 llama-server 추가 payload
- 지식 수와 `knowledge.sqlite` SHA-256
- 사용자 prompt와 `testprompt.md` SHA-256
- `src/**/*.py` 소스 트리 SHA-256
- temperature와 seed
- 블록별 검색 topic, 검색 ID, rank, BM25 점수
- 실제 합성 프롬프트에 주입된 knowledge ID

반복 A/B 예:

```bash
PYTHONPATH=src .venv/bin/python scripts/bench_boards.py \
  --label kn96-seeded --repeats 3 --seed 1000
```

같은 seed 규칙과 코드/지식 hash가 기록되므로 다른 인덱스와 paired 비교할 수 있다.

### 14.4 검색 평가셋

- 평가 데이터: `data/eval/knowledge_retrieval.json`
- 실행기: `scripts/eval_knowledge_retrieval.py`
- 기준 결과: `out/bench_boards/knowledge-retrieval-kn96.json`

```bash
PYTHONPATH=src .venv/bin/python scripts/eval_knowledge_retrieval.py \
  --output out/bench_boards/knowledge-retrieval-kn96.json
```

초기 30질의 Top-3 결과:

- positive Hit@3: 1.000
- macro Recall@3: 0.901
- MRR: 0.914
- 관련 지식이 없는 CAN/USB/LoRa `NO_HIT` 정확도: 0.000

따라서 현재 FTS는 관련 지식 회수는 양호하지만 OR 과매칭 때문에 모르는 영역에서
무관한 결과를 반환한다. 다음 검색 개선의 우선 게이트는 `NO_HIT` 정확도다.

관련성 거부 게이트 적용 후 결과(`knowledge-retrieval-kn96-gated.json`):

- positive Hit@3: 1.000 유지
- macro Recall@3: 0.901 → 0.920
- MRR: 0.914 → 0.938
- `NO_HIT` 정확도: 0.000 → 1.000

게이트는 BM25 절대값에 의존하지 않는다. 질의를 불용어 제거 후 핵심 토큰으로
분리하고, 복수 토큰 질의에서는 엔트리의 statement/condition/tags/type에 최소 두
토큰이 실제로 일치해야 한다. `CAN`은 일반 영문 조동사와 충돌하므로 불용어로
취급한다. 검색 후보를 top-k보다 넓게 가져온 뒤 거부하고 최종 top-k를 구성한다.

### 14.5 STM32G4 결정론 규칙

`normalize.py::ensure_stm32g4_power_network()`가 ST AN5093에 근거해 다음을 강제한다.

- VDD 핀마다 100nF
- MCU 전체에 10uF
- VDDA/VSSA에 100nF + 1uF
- +3V3에서 analog rail로 ferrite bead 분리
- VREF+를 analog rail에 연결

공식 DS12288/AN5093 대조 결과 KiCad 심볼의 핀 누락이 아니라 복합 기능의 GPIO
별칭 표기였다. LQFP64 기준 `PG10(7)=NRST`, `PB8(61)=BOOT0`,
`PA13(49)=SWDIO`, `PA14(50)=SWCLK`이다. 이 의미를 핀 번호 하드코딩이 아니라
GPIO 이름으로 매핑하는 `ensure_stm32g4_system_support()`를 추가했다.

- NRST에 100nF와 SWD header reset 연결
- BOOT0에 10k pulldown과 2핀 service header
- 표준 10핀 ARM SWD: VTref, SWDIO, GND, SWCLK, SWO, NRST
- 기존 모델이 PA13/PA14/PG10/PB8을 다른 net에 잘못 쓴 경우 시스템 기능이 우선

근거: STMicroelectronics AN5093, *Getting started with STM32G4 Series hardware development boards*, power supply/decoupling recommendations.

### 14.6 board03 완전성 검증과 계획 정규화

검색 정확도와 ERC 수치만으로 회로의 완전성을 판단하면 안 된다. board03 단일 생성
스모크에서 ERC가 11건까지 감소했지만, 이미지와 기능별 부품 수를 함께 확인하자
BLDC 드라이버가 전부 빠진 불완전 회로였다. 낮은 ERC가 곧 좋은 설계라는 해석을
막기 위해 다음 검사를 추가했다.

- 프롬프트의 `4축`, motor/BLDC/FOC 표현에서 `bldc_motor_driver × 4` 요구를 복원
- 각 필수 role의 요구 수량, 계획 수량, 실제 생성 수량 기록
- 결과 JSONL에 `required_roles`와 `unplanned_roles` 기록
- 한 블록에 singleton MCU와 반복 encoder가 섞이면 별도 블록으로 분리
- 같은 role을 여러 블록이 소유하면 가장 적합한 블록 하나로 통합
- role이 사라진 빈 블록 제거
- MCU/CAN 같은 singleton의 과다 반복과 driver/encoder의 부족 수량 교정

초기 role 복원 스모크는 107부품, ERC 280건, visual violation 47건이었고 실제 기능
수량도 MCU 6, driver 6, encoder 4, CAN 4로 잘못 증식했다. 동일 계획을 새
`validate_plan()`에 통과시킨 오프라인 결과는 다음과 같이 정규화된다.

```text
POWER       power_supply                 ×1
MCU         controller, can_interface    ×1
RESET       reset_button                 ×1
BLDCMOTOR1  bldc_motor_driver            ×4
ENCODER     encoder                      ×4
```

AS5045B/AS5048 계열에는 전원·NC 핀 처리와 공유 SPI MISO/DO 채널별 47Ω 직렬 저항도
결정론적으로 적용한다. 이 단계의 품질 게이트는 이제 `ERC + visual + role completeness`
세 축이다.

최신 seeded 스모크(`gated-normalized-smoke`, board03, seed 1301)는 실제 회로에서
MCU 1, DRV8311H 4, AS5045B 4, CAN transceiver 1로 반복 수량을 정확히 유지했다.
59부품, ERC 137건, visual issue 13건으로 생성됐지만 전원·퓨즈·TVS·리셋 역할이
계획에서 누락된 사실도 completeness 게이트가 검출했다. 이후 `validate_plan()`은
누락된 전원 진입 역할을 `POWER_REQUIREMENTS` 블록으로 복원하고 `reset_button`은
singleton MCU 블록에 귀속하도록 보강했다. 따라서 다음 전체 A/B는 역할 누락이 0인
실행만 유효 표본으로 간주해야 한다.

보강 후 재실행(`gated-completeness-smoke`, 같은 board/seed)은 6블록, 69부품으로
완료됐고 `unplanned_roles=[]`를 달성했다. 실제 심볼 수는 MCU 1, driver 4,
encoder 4, CAN 1이며 fuse 1, TVS 1, push switch 1, bulk/decoupling capacitor도
포함됐다. ERC는 137→131로 소폭 감소했지만 visual issue는 13→23으로 증가했다.
이는 완전성 복원이 성공했어도 배치 품질은 별도 최적화가 필요함을 뜻한다. 남은
구체적 구조 오류에는 모델이 만든 reset switch의 잘못된 footprint가 포함된다.

### 14.7 A2 배치 리플로우와 라이브러리 정규화

`gated-completeness-smoke`의 visual issue 23건을 좌표 수준에서 재현한 결과 모두
`symbol_overlap`이 아니라 `outside_sheet`였다. 69부품을 기능 그룹별 타일로 배치할
때 145mm 타일 폭, 15.24mm 그룹 간격, 과도한 라벨 여백 때문에 Y 좌표가 약 608mm까지
증가했지만 A2 유효 영역은 Y=385mm까지다.

`place.py::heuristic_place()`를 다음과 같이 조정했다.

- A2의 가로 공간을 활용하도록 타일 폭을 160mm로 확대
- 그룹/행 간격을 7.62/5.08mm로 축소
- 핀 stub을 포함한 라벨 여백을 10.16mm로 보정
- 원점을 `(25.4, 25.4)`로 이동하고 하단 rail 간격 축소
- 기능 그룹과 반복 채널 분리는 그대로 유지

같은 69부품 IR의 정적 재검사는 `outside_sheet 23→0`, `symbol_overlap 0`이며 배치
anchor는 A2 내부에 유지된다. 이는 이미지를 이해한 결과가 아니라 KiCad 심볼의 실제
핀 envelope와 최종 좌표를 검사한 결정론적 리플로우다. 현재 모델은 text-only이므로
SVG를 보았다고 주장하지 않으며, SVG export 성공을 렌더링 oracle로 사용한다.

동시에 part index에는 존재하지만 로컬 KiCad가 불러오지 못하는 vendor TVS/fuse
alias를 발견했다. `normalize_common_symbol_aliases()`는 전기적으로 동일한 2단자
부품을 `Device:D_TVS`, `Device:Fuse`로 치환한다. `SW_Push`처럼 공식 심볼에
footprint filter가 없는 경우에는 검증된 `Button_Switch_SMD:SW_SPST_*` 계열을
pad 번호 대조 후 배정한다. 최종 기록 IR 기준 unknown symbol과 footprint issue는 0이다.

### 14.8 구조 ERC 정규화와 DRV8311H 동작망

board03의 131개 KiCad 위반을 분류하면 pin-not-connected 89,
pin-not-driven 34, power-output 충돌 3, multiple-net-name 4,
isolated-label 1이었다. 원본 IR에는 AC/DC 모듈 한 핀에 CAN/SPI/PWM net 20여 개가
동시에 연결되고 TJA1051의 RXD에 SPI_MOSI와 CAN_TX가 함께 연결된 구조 오류가 있었다.

다음 결정론 규칙을 추가했다.

- `sanitize_known_device_nets()`: 전원 블록으로 누출된 digital net 제거,
  TJA1051 핀별 허용 net whitelist, 물리 핀당 하나의 net 강제
- `ensure_dc_power_entry()`: 4S 배터리 설계에서 잘못 선택된 AC/DC 모듈을 제거하고
  2핀 배터리 입력 → fuse → +12V, TVS/bulk capacitor 보호망으로 재구성
- 실제 power-output이 존재하는 net의 중복/고립 PWR_FLAG 제거
- 사용하지 않는 STM32 GPIO를 명시적인 NC로 처리
- 실제 KiCad stub 길이 7.62mm를 사용한 `label_collision` 검사와 grid 이동

TI DRV8311 데이터시트의 외부 부품 권고와 PWM mode 표에 따라
`ensure_drv8311h_operating_network()`도 추가했다.

- MODE Hi-Z로 3x PWM 선택
- INLx low, GAIN/SLEW ground setting, nSLEEP high
- CP–VM 100nF, CSAREF–AGND 100nF, AVDD–AGND 1uF
- nFAULT 5.1k pull-up
- 채널별 실제 3핀 모터 출력 connector

기존 생성 IR에 규칙만 재적용한 결과는 KiCad 위반 `131→23`, self-ERC error
`107→21`, visual issue `0`, SVG export 성공이다. label 좌표 충돌로 발생하던
power-output 및 multiple-net-name 위반은 0이 됐다. 남은 위반은 current-sense ADC
연결, 일부 PWM drive, CAN connector 및 고립 label이 중심이다.

또한 프롬프트의 `STM32G474RET6`와 달리 모델이 48핀 CBTx를 선택한 사실을 검출했다.
`enforce_requested_stm32_variant()`는 명시적 RET6 요구가 있으면 공식 64핀
`STM32G474RETx` 심볼로 교체하고 기존 연결을 핀 번호가 아닌 핀 이름으로
마이그레이션한다. 이는 4축의 12개 current-sense ADC와 통신/제어 핀을 확보하기 위한
필수 사양 보정이다.

### 14.9 RET6 4축 FOC 핀맵과 ERC-clean 재생성

`apply_stm32g474ret6_foc_pinmap()`은 RET6/LQFP64 전용 고정 핀맵을 적용한다.
핀 번호를 직접 연결하지 않고 KiCad 심볼의 GPIO 이름으로 핀 번호를 찾으며, 한 핀의
기존 net과 NC marker를 제거한 뒤 새 기능 net 하나만 할당한다.

- HRTIM1 12출력: PA8/PA9/PA10/PA11, PB12/PB13/PB14/PB15,
  PC8/PC9/PC6/PC7 → 4축의 PWM_A/B/C
- ADC 입력 12개: PC0–PC5, PA0–PA3, PB0–PB1
- 각 SOA/SOB/SOC: driver output → 47Ω → ADC pin, ADC pin에서 1nF → GND
- SPI1: PA5 SCK, PA6 MISO, PA7 MOSI
- encoder CS: PB4, PB7, PB10, PB11
- FDCAN2: PB5 RX, PB6 TX. PA11을 HRTIM1_CHB2로 유지하기 위해 CAN1
  PA11/PA12 조합을 사용하지 않는다.

`ensure_canfd_bus_protection()`은 CANH/CANL/GND 3핀 connector, 120Ω resistor와
open jumper의 직렬 selectable termination, CANH/CANL 각각의 TVS를 추가한다.
TVS가 rail-to-ground에 놓이는 것은 정상적인 clamp topology이므로 일반 diode의
무저항 rail-short ERC 규칙에서도 명시적으로 제외했다.

113부품 RET6 회로는 A2 한 장에 가독성 있게 들어가지 않아 emitter의 자동 용지 선택을
A1까지 확장했다. 이는 계층 시트 구현 전의 안전한 단일 시트 fallback이며, 시트 밖으로
밀어내지 않는다. 7.62mm label endpoint 충돌 후처리 반복 한도도 대형 회로에 맞게
확장했다.

기존 board03 IR에 `STM32G474RET6` 명시 조건을 추가하여 전체 결정론 규칙을 재적용한
최종 결과(`gated-ret6-pinmap-v4`)는 다음과 같다.

- MCU: STM32G474RETx / value STM32G474RET6
- 12 PWM과 12 current-feedback RC filter 생성
- self-ERC error 0, warning 6
- KiCad ERC 0
- visual issue 0
- SVG export 성공
- IR↔KiCad connectivity round-trip 성공

이 결과는 새 LLM 응답을 선별한 값이 아니라 동일한 기존 생성 IR에 정규화 계층만
적용한 전후 비교다. 따라서 개선의 원인은 모델 확률 변동이 아니라 핀맵·장치 규칙·
배치 규칙 변경이다.

## 15. 계층 시트와 실선 배선 정책

### 15.1 `dvk-mx8m-bsb.pdf`에서 확인한 기준

추가된 25페이지 개발보드 회로도는 하나의 보드라도 SoM, USB-C, battery charger,
power rails, boot configuration, RTC, UART/JTAG, USB hub, µSD, display/camera,
audio, Ethernet, wireless, HDMI, sensors 등 기능별 시트로 분리한다. 중요한 표현 규칙은
다음과 같다.

- 한 기능 회로는 해당 시트 테두리 안에서 읽을 수 있게 완결한다.
- IC 주변의 decoupling, crystal, bias, feedback, protection은 짧은 실선으로 연결한다.
- 다른 시트나 큰 SoC로 이동하는 신호만 net label/hierarchical port로 표현한다.
- 작은 기능은 A4 시트 중앙의 일부만 사용해도 되며 빈 공간을 억지로 채우지 않는다.
- 보드 전체가 한 시트에 들어가는지가 아니라 각 기능 시트가 경계 안에 있는지가 기준이다.

따라서 113부품 회로를 A1 한 장에 넣은 결과는 안전한 진단 fallback일 뿐 최종 목표가
아니다. 최종 BLDC 프로젝트는 최소한 POWER, MCU/CAN/DEBUG, MOTOR_1..4,
ENCODER_1..4 시트로 분리한다. 반복 모터 시트는 동일 topology를 복제하되 채널 net과
reference만 결정론적으로 변경한다.

### 15.2 와이어가 보이지 않았던 원인

현재 `emit.py::build_emit_plan()`은 대부분의 net node에 7.62mm stub과 같은 이름의
local label을 붙인다. 두 핀이 같은 축에서 서로 마주 볼 때만 `_try_direct_wire()`가
실선을 만든다. 이 방식은 연결성에는 유리하지만 사람이 읽는 회로도에서는 부품이
서로 연결되지 않은 것처럼 보인다.

전체 시트 배치 위에서 emitter만 Manhattan/직선 wire로 바꾸는 실험도 수행했지만,
배선용으로 설계되지 않은 shelf placement를 선이 가로지르면서 KiCad ERC가 0에서
36~64건으로 악화됐다. label이 굴절점의 여러 wire에 닿거나 경로가 다른 핀을 통과해
실제 net short가 생겼다. 이 실험 결과는 채택하지 않았고 기존 ERC-clean emitter를
유지했다.

### 15.3 채택한 해결 순서

1. `hierarchy.py::partition_by_function()`으로 모든 부품의 owning sheet를 하나로 결정
2. 한 시트에만 존재하는 net은 local net, 여러 시트를 통과하면 모든 관련 시트의 port
3. MCU/peripheral digital pin은 데이터시트 기반 기본 핀맵을 우선 적용
4. 사용자 핀 지정이 있으면 충돌 검사 후 기본 핀맵을 override
5. op-amp, buck, LDO, oscillator, protection, ADC filter는 local topology를 완성하지
   못하면 해당 시트 생성 실패로 처리
6. local topology를 signal-flow 순서로 다시 배치한 뒤에만 orthogonal wire routing
7. cross-sheet 신호와 global power만 hierarchical label/port 사용
8. 각 child sheet별 경계, wire crossing, label collision, ERC를 검사한 후 root에서 통합

첫 구현으로 `SheetPartition`과 partition validation을 추가했다. 현재 이 계층은 시트
ownership/local-net/port 경계를 계산하고 검증하며, 다음 구현 단계에서 KiCad root
sheet symbol과 child `.kicad_sch` emitter가 이를 소비한다.
