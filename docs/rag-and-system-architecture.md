# CircuitGen RAG 및 전체 시스템 아키텍처

작성일: 2026-08-08  
대상 코드: `/home/hajun/dev_ws/create_circuit`

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
