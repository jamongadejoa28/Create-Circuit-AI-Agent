# CircuitGen 시스템 아키텍처

갱신: 2026-08-20

판정 규칙: [`working-rules.md`](working-rules.md) · 현재 상태: [`STATUS.md`](STATUS.md)

역사 기록(구 아키텍처·계획 문서)은 저장소 밖
[`create_circuit-docs-archive`](../../create_circuit-docs-archive/)에 있습니다.

## 제품 범위

자연어 요구 → 구조화된 **CircuitIR** → KiCad 10 회로도(.kicad_sch) + ERC/SVG/넷리스트.

- **포함:** 요구 추출, 부품 검색, 블록 합성, IR 정규화, 결정론적 배치·배선, 자체 ERC, KiCad CLI 검증, repair 루프
- **미포함:** PCB 동박·DRC, QLoRA/SchGen 승격, 계층 시트 emitter(설계 중)

## 실행 환경

```text
WSL2 / Python 3
  ├─ CircuitGen (src/circuitgen)
  ├─ SQLite: parts.sqlite, knowledge.sqlite
  └─ 테스트·벤치·아티팩트

Windows (또는 동일 호스트)
  ├─ llama.cpp server — Qwen2.5-Coder-7B (temperature 0)
  └─ KiCad 10 CLI — ERC, SVG, netlist
```

## 계층 구조

```text
┌──────────────────────────────────────────────────────────────┐
│ 1. 오케스트레이션 — scripts/run_agent.py, agent.py, webapp   │
├──────────────────────────────────────────────────────────────┤
│ 2. LLM — llm_client.py → Qwen JSON (RequirementSpec, IR…)    │
├──────────────────────────────────────────────────────────────┤
│ 3. 검색 — partindex.py (KiCad 부품), knowledge.py (설계 지식)│
├──────────────────────────────────────────────────────────────┤
│ 4. IR — schemas.py, ir.py, ir_json.py, blocks.py             │
├──────────────────────────────────────────────────────────────┤
│ 5. 정규화·검증 — normalize.py, erc.py, functional_pins.py    │
│              fp_checks.py, interfaces.py, topology.py          │
├──────────────────────────────────────────────────────────────┤
│ 6. EDA 출력 — place.py, emit.py, netlist.py, project.py      │
├──────────────────────────────────────────────────────────────┤
│ 7. 외부 검증 — kicad_cli.py (ERC, SVG, netlist round-trip)   │
├──────────────────────────────────────────────────────────────┤
│ 8. 감사 — audit.py → run.json, out/agent/*.kicad_sch         │
└──────────────────────────────────────────────────────────────┘
```

## End-to-end 흐름

```text
[사용자 프롬프트]
       │
       ▼
1. RequirementSpec — Agent.extract_requirements()
       │
       ▼
2. 범위·전압 정책 검사
       │
       ├─ parts < 5 ──► synthesize_ir()
       └─ parts ≥ 5 ──► plan_blocks() → synthesize_block() × N
                              → instantiate_blocks() → MCU interface
       │
       ▼
3. Agent._normalize() — pin 해석, rail, 풀업, 장치별 NC/전원, 버스 조인
       │
       ▼
4. pipeline.generate() — LLM 없음, 결정론적
       │   self ERC → 배치 → emit → KiCad ERC → SVG → connectivity
       │
       ├─ 실패 ──► repair (최대 MAX_REPAIRS) → IR patch → 4번 반복
       └─ 성공 ──► run.json + 산출물
```

## CircuitIR

LLM과 KiCad 사이의 중간 표현 (`ir.py`).

```text
CircuitIR
  ├─ components[ref] — lib_id, value, footprint, group
  ├─ nets[] — name, nodes[(ref, pin)]
  ├─ nc_pins[(ref, pin)]
  ├─ controller_required, controller_refs[]
  └─ interface_contracts[] — net, owner_group, peer, protocol, required
```

JSON 변환·repair patch: `ir_json.py`. 블록 계획 검증·인스턴스화: `blocks.py`.
controller identity는 RequirementSpec/부품 catalog에서 내려오며 핀 수로 재추측하지 않는다.
BlockPlan interface의 peer/protocol 계약은 인스턴스별 net 이름과 owner group으로 확장된다.

## RAG 지식 vs 결정론적 규칙

| 구분 | RAG 지식 | Python 규칙 |
| --- | --- | --- |
| 저장 | `data/knowledge/*.json` | `src/circuitgen/*.py` |
| 적용 주체 | Qwen (프롬프트 KNOWLEDGE) | 코드 (조건 충족 시 항상) |
| 보장 | 없음 | 단위 테스트 |
| 예 | "IC마다 디커플링" 문장 | `ensure_drv8311_vm_decoupling()` |

지식은 `scripts/build_knowledge_index.py`로 FTS5(`data/knowledge.sqlite`)에 인덱싱됩니다.
`Agent._gather()`가 topic당 최대 2건, 전역 최대 6 snippet을 합성 프롬프트에 넣습니다.
repair 단계에는 KNOWLEDGE를 다시 넣지 않습니다.

고가치 지식은 점진적으로 검수된 `DeviceRule`/`data/rules/*.json`으로 승격합니다.
verified 규칙만 자동 적용; draft는 승격 금지.

## PartIndex

`data/parts.sqlite` — KiCad 심볼·핀·footprint·provenance.
부품 ID와 핀 번호는 회로 정확성의 원천이므로 문서 RAG와 분리합니다.

## Typed interface

`CircuitIR.interface_contracts`는 계획 단계의 **정확성 계약**이다. 예를 들어
`DRIVER2_PWM → peer=controller, protocol=generic_control`이면 해당 owner group과
`controller_refs`의 endpoint가 같은 net에 있어야 한다. I2C/SPI/UART 이름 detector에
없는 제어선도 이 경로로 검증한다.

`interfaces.py` — 완성된 IR과 KiCad 핀 전기 타입에서 ground/power/signal,
driver/consumer 역할을 **추론하지 않고** 파생합니다.
프로토콜(USB/SPI 등)은 net 이름으로 추측하지 않습니다.
배치·run 메트릭용이며 pass/fail 점수가 아닙니다.

물리 바인딩: 사용자 pin intent ↔ KiCad symbol pin ↔ footprint pad가
일치해야 주문 가능 부품으로 취급 (`component_binding_conflict`).

## 연결 문제 3층

| 층 | IR | 회로도에서 보이는 것 | 담당 |
| --- | --- | --- | --- |
| **A** | 핀이 net에 없음 | 부품만 떠 있음 | 합성·정규화·`functional_pins` |
| **B** | IR 연결됨 | wire가 pin에 안 닿음 | `geometry.py`, 배치 |
| **C** | 전기적으로 연결 | stub+label, 선이 끊겨 보임 | `emit.py` |

`connectivity_ok`와 KiCad ERC 0은 C까지 성공으로 본다.
`route_metrics.wired_ratio`는 통계일 뿐 게이트가 아니다. 필수 controller 계약 net은
`critical_stub_nets` 이름 목록과 `critical_wired_ratio`, protocol별 수치로 따로 노출된다.
실선 routing cascade가 실패하면 `EmitPlan.route_failures`가 `stage`, `reason`,
`blocker_nets`를 보존하며, run/benchmark의 `route_failure_reasons`와
`critical_route_failures`에서 원인을 집계한다. `occupied_by_net`은 기존 배선을 제외한
진단 재시도가 성공할 때만 기록하므로 향후 rip-up 후보 net을 구체적으로 가리킨다.

`emit.build_emit_plan`: direct → L → tree(≤8 terminal) → stubs.
세 mode가 모두 sheet-wide `RoutingContext`의 symbol box, foreign pin/stub corridor,
선행 wire cell ownership을 같은 validator로 검사한다. 다만 net 순서는 아직 IR 순서인
one-pass이며 priority와 rip-up은 없다.

## pipeline.generate() 검증 사다리

`pipeline.py` — IR 수신 후 LLM 호출 없음.

1. conceptual symbol 해석
2. PWR_FLAG
3. self ERC (`erc.py` + `functional_pins.check_functional_pin_completeness`)
4. footprint 검사
5. `heuristic_place()` — 기능 그룹 타일
6. semantic geometry QA (`visual.py`) — 시트 경계·겹침·foreign net 접촉
7. KiCad S-expression emit
8. KiCad CLI ERC
9. SVG export
10. netlist round-trip (`compare_connectivity`)

self-ERC error가 있어도 알려진 심볼만으로 draft 회로도를 낼 수 있습니다.
SVG/PNG export 성공은 이미지 품질 판정이 아니며, 사람 또는 별도로 검증된 vision
평가자가 산출물을 확인해야 합니다.

## Repair 루프

파이프라인 실패 시 Qwen `REPAIR_PATCH` JSON → op 필터·적용 → 재검증.
동일 문제 반복 또는 `MAX_REPAIRS` 상한에서 종료.
거부된 라운드의 게이트 사유는 다음 프롬프트에 전달합니다.

## 주요 모듈

| 모듈 | 역할 |
| --- | --- |
| `agent.py` | 요구 추출, 검색, 합성, normalize, repair 오케스트레이션 |
| `normalize.py` | rail, 풀업, 장치별 pin, hub join, 선정 부품 variant |
| `erc.py` | self-ERC 규칙, `check_circuit()` |
| `functional_pins.py` | typed peer 계약, I2C/SPI/serial pin, SPI CS, UART AF completeness (층 A) |
| `emit.py` | 배선 계획, route_metrics |
| `place.py` | 기능 그룹 배치, A2/A1 시트 |
| `topology.py` | 전도·dead component 분석 |
| `kicad_cli.py` | 외부 ERC/SVG/netlist |

## 산출물

- `out/agent/<name>.kicad_sch`, `.kicad_pro`, `.net`, `.erc.json`
- `out/agent/svg/<name>.svg`
- `out/agent/run.json` — prompt, spec, IR, repair, commit·seed·model·지식 hash
- `tests/artifacts/` 아래 SVG/PNG — live benchmark/replay의 검토 대상(추적하지 않음)

## 벤치·캠페인

- 실제 모델 종합: `tests/benchmarks/bench_general.py`
- 정확한 전사 oracle: `tests/benchmarks/bench_transcription.py`
- 순차 비교: `tests/benchmarks/run_sequential_campaign.py`
- 저장된 실제 모델 IR의 backend replay: `tests/benchmarks/replay_model_runs.py`
- 검색 품질: `tests/benchmarks/eval_knowledge_retrieval.py`

각 평가의 보장 범위와 과적합 금지 기준은 `docs/TESTING.md`에 있습니다.

## 의도적 한계

- LlamaIndex/hybrid RAG: 미도입 (FTS + relevance gate)
- 계층 시트 emitter: partition 설계만, KiCad child sheet 미완
- stub+label fallback: 연결성은 확보, 가독성은 별도 과제
- repair에 KNOWLEDGE 미재주입
- SchGen accepted 0 — 승격 금지
