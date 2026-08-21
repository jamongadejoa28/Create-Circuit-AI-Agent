# 현황 — 이 파일만 다음 작업을 적는다

> 판정: [`working-rules.md`](working-rules.md) · 아키텍처: [`ARCHITECTURE.md`](ARCHITECTURE.md)
>
> 캠페인 상세·구 계획·핸드오버는 저장소 밖
> [`create_circuit-docs-archive`](../../create_circuit-docs-archive/) — 에이전트는 참고하지 않는다.

갱신: 2026-08-22 · backend 안정 판정 (replay gated)


## 제품

프롬프트 → 검토 가능한 KiCad 10 회로도. 사용자는 부품을 골라 오고, 이 도구는 설계한다.
전사 모드(넷리스트 있음)는 정답 게이트가 있다. 설계 모드가 제품 본체이며 아직 약하다.
PCB 배치·동박·DRC와 QLoRA는 하지 않는다.

## 캠페인 (요약)

고정 입력: `tests/eval/sequential_campaign_v1.json` · 실행: `tests/benchmarks/run_sequential_campaign.py`.

최신 측정 기준: `ko-step-024-cs-gnd-s2` / `ko-step-025-wp-nc-s2`.
**Backend replay (LLM 없음):** `tests/artifacts/benchmarks/replay/backend-stability-20260822/`
(동일 IR: `backend-20260822a`).

| 케이스 | wired | critical_wired | critical_stubs | 비고 |
| --- | --- | --- | --- | --- |
| 001 LED | 1.0 | — | [] | clean |
| 002 LDO | 1.0 | — | [] | self-ERC PWROUT — **구 IR**, C층 아님 |
| 003 opamp | 1.0 | — | [] | clean |
| 004 타이머 | 1.0 | — | [] | self-ERC 핀 미연결 — **구 IR** |
| 005 오디오 | 1.0 | — | [] | oriented occupancy |
| 006 I2C | 1.0 | 1.0 | [] | clean |
| 007 SPI | 1.0 | 1.0 | [] | self-ERC 심볼/controller 선언 — **구 IR** |

### Backend 안정 판정 (2026-08-22, LLM 없음)

| 게이트 | 결과 |
| --- | --- |
| pytest (router/tree/contracts/repair/visual/geometry/functional/pipeline/erc/interfaces/replay + netlist/chain/hier/normalize/contracts/blocks) | **191 passed** |
| ko-step-024 replay ×7 `wired_ratio` | **전부 1.0** |
| `critical_stub_nets` / `critical_wired_ratio` | **전부 OK** (critical 있는 006·007 = 1.0) |
| `connectivity_ok` / runtime / `visual_issues` | **전부 OK** (visual 0) |
| preview PNG | 7/7 생성 (가독성 자동승인 아님) |
| self-ERC 0 | 4/7 — 실패 3건은 **저장된 구 IR** (C층 회귀 아님) |

**판정: C층(place/emit/router)은 시퀀셜 캠페인 재개에 충분.**  
PNG는 사람이 보고, A층 self-ERC는 새 LLM IR에서 다시 측정한다.

## 최근 코드

- **typed interface @ RequirementSpec** · **critical-first** · **TREE_MAX_NODES=16**
- **limited rip-up** · **route-aware local placement** · **route-debug SVG**
- **legacy bus-first routing** — 계약 없는 구 IR에서도 `is_i2c_net`/`is_spi_net`
  멤버십으로 버스 net을 emit 우선순위에 올림. metrics critical도 동일 기준.
- **legacy controller** — `functional_pins`가 `_recorded_controller_refs`(핀함수 테이블)를 사용.
- **SCLK → SCK** — `_SPI_TOKEN_TO_AF` / `pin_name_spi_role`에 일반 SPI 시계 별칭.
- **bus line-rank** — 같은 tier 안에서 SCK→MOSI→MISO→CS; GPIO CS도 SPI 기기면 tier 0.
- **equal-tier swap** — 동순위 `occupied_by_net`일 때 blocker를 잠깐 풀고,
  **둘 다** 실선되면만 유지(한쪽 stub로 바꾸지 않음).
- **oriented wire occupancy** — 같은 방향 cell 재사용만 거부. 직교 mid-segment 교차는
  KiCad에서 단락이 아니므로 허용. `visual` QA도 T접점/평행 겹침만 short로 본다.

## 연결 문제 3층 (A / B / C)

| 층 | IR | 사람이 보는 회로도 | 원인 |
| --- | --- | --- | --- |
| **A. IR 미연결** | 핀이 `ir.nets`에 없음 | 부품만 떠 있음 | 합성·정규화가 net membership을 안 만듦 |
| **B. Geometry** | IR 연결됨 | 거의 붙어 보이거나 KiCad ERC 단선 | pin 좌표 ≠ wire endpoint |
| **C. Label fallback** | 전기적으로 연결 | **선이 끊겨 보임** | `emit` stub+동일 net label |

A층은 닫힘. C층: critical-first + rip-up + local placement + bus-first(legacy).

## 제품 규칙

- verified: `data/rules/ldo_linear_regulator.json` 하나
- draft rule 파일은 보관하지 않는다
- 지식: `data/knowledge/manufacturer-datasheets.json`
- 전압 한도: `data/device_limits.json`

## 다음 작업 (규칙 9)

1. ~~typed interfaces · critical-first · TREE 16 · rip-up · local placement · overlay~~
2. ~~ko-step-024 IR replay~~ — I2C critical 실선.
3. ~~동일 tier SPI occupancy~~ — line-rank + equal-tier swap; 007 critical/wired 1.0.
4. ~~005 IN occupied_by_net~~ — oriented occupancy로 해결.
5. ~~밀도/충돌(대형 multi-net)~~ — 직교 교차 허용으로 005/006 wired 1.0.
6. ~~backend 안정 확인~~ — 위 판정. C층 통과.
7. **새 LLM 시퀀셜 캠페인** (`run_sequential_campaign.py` + llama-server) —
   고정 입력 `sequential_campaign_v1.json`, 결과는 artifacts에 남기고 PNG는 사람 검토.
8. (선택) 동일 방향 corridor placement 분산 — 필요할 때만.

## 데이터·학습

제조사 PDF: `data/datasheets/`. QLoRA 없음. SchGen accepted **0**.
학습/SFT로 넘어가지 않는다.

## 하지 않을 일

- ERC/벤치 점수 자랑 · 패턴 apply_when · SchGen 승격 · 회로명 특례
- I2C/SPI/부품별 A층 normalize 규칙 추가
- `contracts.infer_contracts` 키워드 확장
- 1–5번 시퀀셜을 기본 작업 큐로 되돌리기
